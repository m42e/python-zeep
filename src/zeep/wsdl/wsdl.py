from __future__ import print_function

import os
from collections import OrderedDict

import six
from lxml import etree
from lxml.etree import QName

from zeep.parser import parse_xml
from zeep.utils import findall_multiple_ns
from zeep.wsdl import definitions, soap
from zeep.xsd import Schema

NSMAP = {
    'wsdl': 'http://schemas.xmlsoap.org/wsdl/',
}


class WSDL(object):
    def __init__(self, location, transport):
        self.location = location
        self.transport = transport

        # Dict with all definition objects within this WSDL
        self._definitions = {}

        # Dict with internal schema objects, used for lxml.ImportResolver
        self._schema_references = {}

        root_definitions = Definitions(self, location)
        root_definitions.resolve_imports()

        # Make the wsdl definitions public
        self.schema = root_definitions.schema
        self.messages = root_definitions.messages
        self.port_types = root_definitions.port_types
        self.bindings = root_definitions.bindings
        self.services = root_definitions.services

    def __repr__(self):
        return '<WSDL(location=%r)>' % self.location

    def dump(self):
        type_instances = self.schema.types
        print('Types:')
        for type_obj in sorted(type_instances, key=lambda k: six.text_type(k)):
            print(' ' * 4, six.text_type(type_obj))

        print('')

        for service in self.services.values():
            print(six.text_type(service))
            for port in service.ports.values():
                print(' ' * 4, six.text_type(port))
                print(' ' * 8, 'Operations:')
                for operation in port.binding._operations.values():
                    print('%s%s' % (' ' * 12, six.text_type(operation)))
                print('')


class Definitions(object):
    def __init__(self, wsdl, location):
        self.wsdl = wsdl
        self.location = location

        self.schema = None
        self.port_types = {}
        self.messages = {}
        self.bindings = {}
        self.services = OrderedDict()

        self.imports = {}

        if location.startswith(('http://', 'https://')):
            response = self.wsdl.transport.load(location)
            doc = self._parse_content(response)
        else:
            with open(location, mode='rb') as fh:
                doc = self._parse_content(fh.read())

        self.target_namespace = doc.get('targetNamespace')
        self.wsdl._definitions[self.target_namespace] = self
        self.nsmap = doc.nsmap

        # Process the definitions
        self.parse_imports(doc)

        self.schema = self.parse_types(doc)
        self.messages = self.parse_messages(doc)
        self.port_types = self.parse_ports(doc)
        self.bindings = self.parse_binding(doc)
        self.services = self.parse_service(doc)

    def __repr__(self):
        return '<Definitions(location=%r)>' % self.location

    def resolve_imports(self):
        """
            A -> B -> C -> D

            Items defined in D are only available in C, not in A or B.

        """
        for namespace, definition in self.imports.items():
            self.merge(definition, namespace)

        imports = self.imports.copy()
        self.imports = {}

        for definition in imports.values():
            definition.resolve_imports()

        for message in self.messages.values():
            message.resolve(self)

        for port_type in self.port_types.values():
            port_type.resolve(self)

        for binding in self.bindings.values():
            binding.resolve(self)

        for service in self.services.values():
            service.resolve(self)

    def _parse_content(self, content):
        return parse_xml(
            content, self.wsdl.transport, self.wsdl._schema_references)

    def merge(self, other, namespace):
        """Merge another `WSDL` instance in this object."""
        def filter_namespace(source, namespace):
            return {
                k: v for k, v in source.items()
                if k.startswith('{%s}' % namespace)
            }

        if not self.schema:
            self.schema = other.schema

        self.port_types.update(filter_namespace(other.port_types, namespace))
        self.messages.update(filter_namespace(other.messages, namespace))
        self.bindings.update(filter_namespace(other.bindings, namespace))
        self.services.update(filter_namespace(other.services, namespace))

        if namespace not in self.wsdl._definitions:
            self._definitions[namespace] = other

    def parse_imports(self, doc):
        """Import other WSDL documents in this document.

        Note that imports are non-transitive, so only import definitions
        which are defined in the imported document and ignore definitions
        imported in that document.

        This should handle recursive imports though:

            A -> B -> A
            A -> B -> C -> A

        """
        for import_node in doc.findall("wsdl:import", namespaces=NSMAP):
            location = import_node.get('location')
            namespace = import_node.get('namespace')

            if '://' not in location and not os.path.isabs(location):
                location = os.path.join(os.path.dirname(self.location), location)

            if namespace in self.wsdl._definitions:
                self.imports[namespace] = self.wsdl._definitions[namespace]
            else:
                wsdl = Definitions(self.wsdl, location)
                self.imports[namespace] = wsdl

    def parse_types(self, doc):
        """Return a `types.Schema` instance.

        Note that a WSDL can contain multiple XSD schema's. The schemas can
        reference import each other using xsd:import statements.

            <definitions .... >
                <types>
                    <xsd:schema .... />*
                </types>
            </definitions>

        """
        namespace_sets = [
            {'xsd': 'http://www.w3.org/2001/XMLSchema'},
            {'xsd': 'http://www.w3.org/1999/XMLSchema'},
        ]

        types = doc.find('wsdl:types', namespaces=NSMAP)
        if types is None:
            return

        schema_nodes = findall_multiple_ns(types, 'xsd:schema', namespace_sets)
        if not schema_nodes:
            return None

        # FIXME: This fixes `test_parse_types_nsmap_issues`, lame solution...
        schema_nodes = [
            self._parse_content(etree.tostring(schema_node))
            for schema_node in schema_nodes
        ]

        for schema_node in schema_nodes:
            tns = schema_node.get('targetNamespace')
            self.wsdl._schema_references['intschema+%s' % tns] = schema_node

        # Only handle the import statements from the 2001 xsd's for now
        import_tag = QName('http://www.w3.org/2001/XMLSchema', 'import').text
        for schema_node in schema_nodes:
            for import_node in schema_node.findall(import_tag):
                if import_node.get('schemaLocation'):
                    continue
                namespace = import_node.get('namespace')
                import_node.set('schemaLocation', 'intschema+%s' % namespace)

        schema_node = schema_nodes[0]

        return Schema(
            schema_node, self.wsdl.transport, self.wsdl._schema_references)

    def parse_messages(self, doc):
        """
            <definitions .... >
                <message name="nmtoken"> *
                    <part name="nmtoken" element="qname"? type="qname"?/> *
                </message>
            </definitions>
        """
        result = {}
        for msg_node in doc.findall("wsdl:message", namespaces=NSMAP):
            msg = definitions.AbstractMessage.parse(self, msg_node)
            result[msg.name.text] = msg
        return result

    def parse_ports(self, doc):
        """Return dict with `PortType` instances as values

            <wsdl:definitions .... >
                <wsdl:portType name="nmtoken">
                    <wsdl:operation name="nmtoken" .... /> *
                </wsdl:portType>
            </wsdl:definitions>
        """
        result = {}
        for port_node in doc.findall('wsdl:portType', namespaces=NSMAP):
            port_type = definitions.PortType.parse(self, port_node)
            result[port_type.name.text] = port_type
        return result

    def parse_binding(self, doc):
        """
            <wsdl:definitions .... >
                <wsdl:binding name="nmtoken" type="qname"> *
                    <-- extensibility element (1) --> *
                    <wsdl:operation name="nmtoken"> *
                       <-- extensibility element (2) --> *
                       <wsdl:input name="nmtoken"? > ?
                           <-- extensibility element (3) -->
                       </wsdl:input>
                       <wsdl:output name="nmtoken"? > ?
                           <-- extensibility element (4) --> *
                       </wsdl:output>
                       <wsdl:fault name="nmtoken"> *
                           <-- extensibility element (5) --> *
                       </wsdl:fault>
                    </wsdl:operation>
                </wsdl:binding>
            </wsdl:definitions>
        """
        result = {}
        for binding_node in doc.findall('wsdl:binding', namespaces=NSMAP):
            # Detect the binding type
            if soap.Soap11Binding.match(binding_node):
                binding = soap.Soap11Binding.parse(self, binding_node)
            elif soap.Soap12Binding.match(binding_node):
                binding = soap.Soap12Binding.parse(self, binding_node)
            # Still in development
            # elif http.HttpBinding.match(binding_node):
            #     binding = http.HttpBinding.parse(self, binding_node)
            else:
                continue

            binding.wsdl = self
            result[binding.name.text] = binding
        return result

    def parse_service(self, doc):
        """
            <wsdl:definitions .... >
                <wsdl:service .... > *
                    <wsdl:port name="nmtoken" binding="qname"> *
                       <-- extensibility element (1) -->
                    </wsdl:port>
                </wsdl:service>
            </wsdl:definitions>
        """
        result = OrderedDict()
        for service_node in doc.findall('wsdl:service', namespaces=NSMAP):
            service = definitions.Service.parse(self, service_node)
            result[service.name.text] = service
        return result
