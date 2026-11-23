# Copyright (c) 2024, itsdave GmbH and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase


class TestAIQuery(FrappeTestCase):
	pass


class TestSupplierPartyLinks(FrappeTestCase):
    def _supplier(self, name):
        return frappe.get_doc({"doctype": "Supplier", "supplier_name": name, "supplier_group": "", "supplier_type": "Company"}).insert()

    def setUp(self):
        from ai_workflows.ai_workflows.doctype.ai_query import ai_query

        self.q = ai_query
        self.other = self._supplier("_Test AIQ Other")
        self.new = self._supplier("_Test AIQ New")
        self.foreign_contact = frappe.get_doc({"doctype": "Contact", "first_name": "Office", "company_name": "_Test AIQ Other", "links": [{"link_doctype": "Supplier", "link_name": self.other.name}]}).insert()
        self.foreign_address = frappe.get_doc({"doctype": "Address", "address_title": "Other", "address_line1": "Teststr. 1", "city": "Testort", "pincode": "12345", "country": "Germany", "links": [{"link_doctype": "Supplier", "link_name": self.other.name}]}).insert()
        self.details = {"SupplierContactPerson": "Office", "SupplierAddress": {"Street": "Teststr. 1", "City": "Testort", "PostalCode": "12345", "Country": "DE"}}

    def tearDown(self):
        frappe.db.rollback()

    def test_foreign_address_and_contact_untouched(self):
        s = self.q.create_or_update_address(self.new, self.details)
        s = self.q.create_or_update_contact(s, self.details, s.supplier_primary_address)
        self.assertNotEqual(s.supplier_primary_address, self.foreign_address.name)
        self.assertNotEqual(s.supplier_primary_contact, self.foreign_contact.name)
        self.assertEqual([l.link_name for l in frappe.get_doc("Address", self.foreign_address.name).links], [self.other.name])
        self.assertEqual(frappe.db.get_value("Contact", self.foreign_contact.name, "company_name"), "_Test AIQ Other")

    def test_own_records_reused_and_wrong_primary_fixed(self):
        s = self.q.create_or_update_address(self.new, self.details)
        s = self.q.create_or_update_contact(s, self.details, s.supplier_primary_address)
        s.save()
        own_contact, own_address = s.supplier_primary_contact, s.supplier_primary_address
        s = self.q.create_or_update_address(s, self.details)
        s = self.q.create_or_update_contact(s, {"SupplierContactPerson": "Office", "SupplierContactEmail": "x@example.com"}, None)
        self.assertEqual((s.supplier_primary_contact, s.supplier_primary_address), (own_contact, own_address))
        self.assertEqual(len(self.q._linked_names("Contact", s.name)), 1)
        s.supplier_primary_contact = self.foreign_contact.name
        s = self.q.create_or_update_contact(s, {}, None)
        self.assertEqual(s.supplier_primary_contact, own_contact)
