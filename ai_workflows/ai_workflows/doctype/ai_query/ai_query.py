# Copyright (c) 2024, itsdave GmbH and contributors
# For license information, please see license.txt

import json
import re
import frappe
import random
import string
from frappe.model.document import Document
from frappe.utils.password import get_decrypted_password
from frappe.utils import getdate, validate_email_address, today
from openai import OpenAI
from erpnext.controllers.accounts_controller import get_taxes_and_charges

if 'frappe_goes_paperless' in frappe.get_installed_apps():
    from frappe_goes_paperless.frappe_goes_paperless.tools import get_paperless_settings


class AIQuery(Document):
    pass

# Get settings from AI doctype settings
def get_ai_settings(doc_ai):
    doc_ai = frappe.get_doc("AI", doc_ai)

    api_key = get_decrypted_password(
        doctype="AI", name=doc_ai.name, fieldname="api_key", raise_exception=False
    )
    return api_key


def get_country(code_country):
    # Get country by code
    country = frappe.db.get_value("Country", {"code": code_country.lower()})
    return country


@frappe.whitelist()
def call_ai(ai, prompt, doc, background=True):
    # Universal AI
    print("Starting function to get ai ...")
    if background == "false":
        background = False
    # Get AI
    try:
        doc_ai = frappe.get_doc("AI", ai)
    except frappe.DoesNotExistError:
        return "AI not found!"
    if doc_ai.interface == "openAI":
        if background:
            jobId = frappe.enqueue(
                "frappe_goes_paperless.frappe_goes_paperless.doctype.paperless_document.paperless_document.use_openai",
                queue="short",
                now=False,
                doc=doc,
                prompt=prompt,
                ai_config=doc_ai.name,
                background=True,
            )
            return jobId
        else:
            do_ai = use_openai(doc, prompt, doc_ai.name, False)
            if do_ai:
                return do_ai


def use_openai(doc, prompt, ai_name, background=True):
    print("Initiate get ai data ...")

    client = OpenAI(api_key=get_ai_settings(ai_name))
    doc = json.loads(doc)

    # get prompt
    prompt = frappe.get_doc("AI Prompt", prompt)
    # check AI mode
    if prompt.ai_output_mode == "Structured Output (JSON)":
        effective_prompt = f"{prompt.long_text_fnbe}\n\n{doc.get('document_fulltext')}"
        json_schema = (
            json.loads(prompt.json_scema)
            if type(prompt.json_scema) == str
            else prompt.json_scema
        )
        chat_response = client.chat.completions.create(
            model="gpt-4o-2024-08-06",
            messages=[
                {
                    "role": "system",
                    "content": "You are a wizard that generates invoice details in JSON format.",
                },
                {"role": "user", "content": effective_prompt},
            ],
            functions=[
                {
                    "name": "generate_invoice",
                    "description": "Generates an invoice based on the provided schema.",
                    "parameters": json_schema,
                }
            ],
            function_call={"name": "generate_invoice"},
        )
        if chat_response.choices:
            function_call = chat_response.choices[0].message.function_call
            if function_call:
                resp = function_call.arguments
            else:
                resp = ""
        else:
            resp = ""
    # else if AI mode is Chat or None
    else:
        # concat fulltext and prompt
        effective_prompt = f"{prompt.long_text_fnbe}\n\n{doc.get('document_fulltext')}"
        # init chat
        chat_completion = client.chat.completions.create(
            messages=[
                {
                    "role": "user",
                    "content": effective_prompt,
                }
            ],
            model="chatgpt-4o-latest",
        )
        if chat_completion.choices:
            resp = chat_completion.choices[0].message.content
        else:
            resp = ""

    # add doctype AI Query
    new_query = frappe.new_doc("AI Query")
    new_query.document_type = prompt.for_doctype
    new_query.paperless_doc = doc.get("name")
    new_query.ai = ai_name
    new_query.ai_prompt_template = prompt
    new_query.effective_prompt = effective_prompt
    new_query.ai_response = resp.strip() if resp else ""

    json_pattern = r"\{.*\}"
    if resp is not None:
        matches = re.findall(json_pattern, resp, re.DOTALL)
    else:
        matches = []
    if matches:
        json_content = matches[0]
        try:
            data = json.loads(json_content)
            formatted_json = json.dumps(data, indent=2)
            new_query.ai_response_json = formatted_json
        except json.JSONDecodeError as e:
            new_query.ai_response_json = f"Error on decode JSON: {e}"
    else:
        new_query.ai_response_json = "The content is not in JSON format"
    # save query ai
    new_query.save()
    # Load document paperless and set status
    doc_paperless = frappe.get_doc("Paperless Document", doc.get("name"))
    doc_paperless.status = "AI-Response-Received"
    doc_paperless.save()
    frappe.db.commit()
    # Return success
    if background:
        frappe.publish_realtime(
            "msgprint_end", "Response received successfully, fields updated!"
        )
        return True
    else:
        return f'AI query sucessfull. <a href="{frappe.utils.get_url()}/app/ai-query/{new_query.name}">Check out response</a>.'

@frappe.whitelist()
def create_supplier(doc):
    doc = json.loads(doc)

    # Extract JSON data
    json_data = doc.get("ai_response_json")
    if not isinstance(json_data, str):
        return "Invalid JSON format"
    try:
        json_data = json.loads(json_data)
    except:
        return "Invalid JSON format"

    invoice_details = json_data.get("InvoiceDetails")
    supplier_ust_id = invoice_details.get("SupplierUstId")
    supplier_name = invoice_details.get("SupplierName")

    # First, try to find the supplier by tax_id (SupplierUstId)
    supplier = None
    if supplier_ust_id:
        supplier = frappe.db.get_value("Supplier", {"tax_id": supplier_ust_id}, "name")

    # If supplier not found by tax_id, fall back to supplier_name
    if not supplier:
        supplier = frappe.db.get_value(
            "Supplier", {"supplier_name": supplier_name}, "name"
        )

    # When its there, we need to fetch the Document
    if supplier:
        supplier = frappe.get_doc("Supplier", supplier)

    if not supplier:
        # Create a new supplier if not found
        supplier = frappe.get_doc(
            {
                "doctype": "Supplier",
                "supplier_name": supplier_name,
                "tax_id": supplier_ust_id,  # Save the SupplierUstId as tax_id
                "supplier_group": "",
                "supplier_type": "Company",
            }
        )
        supplier.insert()
        print(f"Supplier '{supplier.supplier_name}' created successfully!")
        return_msg = "Supplier created successfully"
    else:
        # Update the existing supplier with the tax_id if not already set
        supplier_doc = frappe.get_doc("Supplier", supplier)
        if supplier_ust_id and not supplier_doc.tax_id:
            supplier_doc.tax_id = supplier_ust_id
            supplier_doc.save()
        return_msg = "Supplier already exists, updated successfully"

    # Update AI Query with supplier
    ai_query_doc = frappe.get_doc("AI Query", doc.get("name"))
    ai_query_doc.supplier = supplier.name
    ai_query_doc.save()


    # Create or update Address
    supplier = create_or_update_address(supplier, invoice_details)

    # Create or update Contact with the address reference
    supplier = create_or_update_contact(supplier, invoice_details, supplier.supplier_primary_address)

    # Commit database and return message
    supplier.save()

    return return_msg


def create_or_update_contact(supplier, invoice_details, address_name):
    print("create_or_update_contact")
    contact_person = invoice_details.get(
        "SupplierContactPerson", supplier.supplier_name
    ).split(" ")
    contact_phone = invoice_details.get("SupplierContactPhone", "")
    contact_email = invoice_details.get("SupplierContactEmail", "")

    

    # Fetch the contact based on the first and last name
    contact_name = frappe.db.get_value(
        "Contact",
        {
            "first_name": contact_person[0],
            "last_name": contact_person[1] if len(contact_person) > 1 else "",
        },
        "name"  # Fetch the contact name, not the full document
    )

    # If no contact found, create a new one
    if not contact_name:
        contact = frappe.new_doc("Contact")
        contact.first_name = contact_person[0]
        contact.last_name = contact_person[1] if len(contact_person) > 1 else ""
        contact.company_name = supplier.supplier_name  # Link the contact to the supplier's name
        contact.address = address_name  # Link the contact to the address

        contact.append(
            "links", {"link_doctype": "Supplier", "link_name": supplier.name}
        )

        if contact_phone:
            contact.append("phone_nos", {"phone": contact_phone, "is_primary_phone": 1})

        if contact_email:
            if validate_email_address(contact_email):
                contact.append("email_ids", {"email_id": contact_email, "is_primary": 1})

        contact.insert()
        contact_name = contact.name  # Ensure we set the correct contact name
    else:
        # Fetch the existing contact as a document
        contact = frappe.get_doc("Contact", contact_name)
        contact.company_name = supplier.supplier_name  # Ensure the company name is updated
        contact.address = address_name  # Ensure the address is linked

        if contact_phone:
            existing_phone = next(
                (
                    phone
                    for phone in contact.phone_nos
                    if phone.phone == contact_phone
                ),
                None,
            )
            if not existing_phone:
                contact.append(
                    "phone_nos",
                    {
                        "phone": contact_phone,
                        "is_primary_phone": 1 if not contact.phone_nos else 0,
                    },
                )

        if contact_email:
            existing_email = next(
                (
                    email
                    for email in contact.email_ids
                    if email.email_id == contact_email
                ),
                None,
            )
            if not existing_email:
                contact.append(
                    "email_ids",
                    {
                        "email_id": contact_email,
                        "is_primary": 1 if not contact.email_ids else 0,
                    },
                )

        if not any(link.link_name == supplier.name for link in contact.links):
            contact.append(
                "links", {"link_doctype": "Supplier", "link_name": supplier.name}
            )
        contact.save()

    # Assign the contact name to supplier's primary contact
    supplier.supplier_primary_contact = contact_name
    return supplier



def create_or_update_address(supplier, invoice_details):
    print("create_or_update_address")
    # Fetch the address name if it exists
    address_name = frappe.db.get_value(
        "Address",
        {
            "address_line1": invoice_details["SupplierAddress"]["Street"],
            "city": invoice_details["SupplierAddress"]["City"],
            "pincode": invoice_details["SupplierAddress"]["PostalCode"],
            "country": get_country(invoice_details["SupplierAddress"]["Country"]),
        },
    )

    if not address_name:
        print("Creating new address")
        # Create a new address document
        address_doc = frappe.new_doc("Address")
        address_doc.address_title = f"{supplier.supplier_name} - Main Address"
        address_doc.address_line1 = invoice_details["SupplierAddress"]["Street"]
        address_doc.city = invoice_details["SupplierAddress"]["City"]
        address_doc.pincode = invoice_details["SupplierAddress"]["PostalCode"]
        address_doc.country = get_country(invoice_details["SupplierAddress"]["Country"])
        address_doc.append(
            "links", {"link_doctype": "Supplier", "link_name": supplier.name}
        )
        address_doc.insert()
    else:
        # If the address exists, load the address document
        address_doc = frappe.get_doc("Address", address_name)
        # Check if the supplier link already exists
        if not any(link.link_name == supplier.name for link in address_doc.links):
            address_doc.append(
                "links", {"link_doctype": "Supplier", "link_name": supplier.name}
            )
            address_doc.save()

    # Update the supplier's primary address with the name of the address
    supplier.supplier_primary_address = address_doc.name
    return supplier



@frappe.whitelist()
def create_purchase_invoice(doc):
    doc = json.loads(doc)

    # Get JSON data
    json_data_str = doc.get("ai_response_json")

    # Validate JSON format
    if not isinstance(json_data_str, str):
        frappe.throw("Invalid JSON format")
    try:
        json_data = json.loads(json_data_str)
    except json.JSONDecodeError:
        frappe.throw("Invalid JSON format")

    try:
        # Extract necessary fields
        supplier_name = doc.get("supplier")
        invoice_details = json_data.get("InvoiceDetails", {})
        items_purchased = json_data.get("ItemsPurchased", {}).get("ItemList", [])
        payment_information = json_data.get("PaymentInformation", {})

        # Check for missing mandatory keys
        if not supplier_name:
            frappe.throw("Supplier name is missing in the request.")

        if not invoice_details:
            frappe.throw("Invoice details are missing in the JSON data.")

        if not items_purchased:
            frappe.throw("Items purchased are missing in the JSON data.")

        if "InvoiceNumber" not in invoice_details:
            frappe.throw("InvoiceNumber is missing in the InvoiceDetails.")

        if "InvoiceDate" not in invoice_details:
            frappe.throw("InvoiceDate is missing in the InvoiceDetails.")

        # Get supplier from database
        supplier = frappe.db.get_value("Supplier", {"name": supplier_name})
        if not supplier:
            frappe.throw(f"Supplier '{supplier_name}' does not exist.")

        supplier_doc = frappe.get_doc("Supplier", supplier)

        # Check if Purchase Invoice already exists for this InvoiceNumber and Supplier, ignoring canceled invoices
        existing_purchase_invoice = frappe.db.exists(
            "Purchase Invoice",
            {
                "bill_no": invoice_details["InvoiceNumber"],
                "supplier": supplier_doc.name,
                "docstatus": ["!=", 2],  # Exclude canceled invoices
            },
        )
        if existing_purchase_invoice:
            frappe.msgprint(
                f"Purchase Invoice with Invoice Number '{invoice_details['InvoiceNumber']}' already exists for supplier '{supplier_doc.name}'."
            )
            return

       # Extract due date and posting date
        invoice_date = invoice_details.get("InvoiceDate")
        due_date = payment_information.get("PaymentDueDate", invoice_date)

        # Validate and parse invoice_date
        try:
            invoice_date_parsed = getdate(invoice_date)
        except Exception:
            frappe.msgprint(f"Invalid InvoiceDate '{invoice_date}', using current date.")
            invoice_date = today()
            invoice_date_parsed = getdate(invoice_date)

        # Validate and parse due_date
        try:
            due_date_parsed = getdate(due_date)
        except Exception:
            frappe.msgprint(f"Invalid PaymentDueDate '{due_date}', using current date.")
            due_date = today()
            due_date_parsed = getdate(due_date)

        # Ensure due_date is not before invoice_date
        if due_date_parsed < invoice_date_parsed:
            due_date = invoice_date

        # Create a new Purchase Invoice

        purchase_invoice = frappe.get_doc(
            {
                "doctype": "Purchase Invoice",
                "supplier": supplier_doc.name,
                "due_date": due_date,
                "bill_no": invoice_details["InvoiceNumber"],
                "bill_date": invoice_details["InvoiceDate"],
                "items": [],
            }
        )
        # Get destination Item Group for new Items
        item_group = frappe.db.get_single_value(
            "AI Workflow Purchase Invoice Settings", "default_item_group"
        )

        # Check and add items to the Purchase Invoice
        for item in items_purchased:

            item_number = item.get("ItemNumber", None)
            # If ItemNumber is missing, generate a new one
            if not item_number:
                item_number = generate_item_number()

            # Ensure the item exists, create if not
            item_doc_name = create_or_get_item(
                item_number,
                item["ItemName"],
                item["Description"],
                supplier_doc.name,
                item_group=item_group,
            )
            item_doc = frappe.get_doc("Item", item_doc_name)

            # Check if quantity and unit price are zero
            if float(item["Quantity"]) == 0 or float(item["UnitPrice"]) == 0:
                calculated_rate = 0
                discount_amount = 0
                discount_percentage = 0
            else:
                # Calculate the rate per unit after discount
                calculated_rate = float(item["Total"]) / float(item["Quantity"])

                # Calculate the discount amount
                discount_amount = (
                    float(item["UnitPrice"]) * float(item["Quantity"])
                ) - float(item["Total"])

                # Calculate the discount percentage
                discount_percentage = (
                    discount_amount
                    / (float(item["UnitPrice"]) * float(item["Quantity"]))
                ) * 100

            # Create a Purchase Invoice Item entry
            po_item = create_purchase_invoice_doc_item(
                item_doc.name,
                float(item["Quantity"]),
                item_doc.stock_uom,
                calculated_rate,
            )

            # Set the price_list_rate to the UnitPrice
            po_item.price_list_rate = float(item["UnitPrice"])

            # Set the discount_amount
            po_item.discount_amount = discount_amount

            # Set the discount_percentage
            po_item.discount_percentage = discount_percentage

            # Set the rate after discount
            po_item.rate = calculated_rate

            # Calculate and set the amount and base_amount fields
            po_item.amount = calculated_rate * float(item["Quantity"])
            po_item.base_rate = calculated_rate  # Assuming base currency is the same
            po_item.base_amount = po_item.amount  # Assuming base currency is the same

            # Add the item to the Purchase Invoice without checking for duplicates
            purchase_invoice.append("items", po_item)

        # Save the Purchase Invoice, which fills taxes_and_charges
        purchase_invoice.insert()

        # Check if the InvoiceDate is different from today's date, and set posting_date if so
        if getdate(purchase_invoice.posting_date) != getdate(
            invoice_details["InvoiceDate"]
        ):
            # If different, enable set_posting_time and update posting_date
            purchase_invoice.set_posting_time = 1
            purchase_invoice.posting_date = invoice_details["InvoiceDate"]
            purchase_invoice.save()

        # --- Begin code to handle PaymentMethod and Payment Schedule ---

        # Extract PaymentMethod from payment_information
        payment_method_string = payment_information.get("PaymentMethod")

        # Translate PaymentMethod using AI Workflow Payment Terms Assignment
        payment_term = None
        ai_settings = frappe.get_single("AI Workflow Purchase Invoice Settings")
        if ai_settings.payment_terms_assignments:
            for assignment in ai_settings.payment_terms_assignments:
                if (
                    assignment.ai_response_paymentmethod_string.lower().strip()
                    == payment_method_string.lower().strip()
                ):
                    payment_term = assignment.payment_term
                    break

        if not payment_term:
            frappe.throw(
                f"No payment term found for payment method '{payment_method_string}'"
            )

        # Validate that payment_term exists
        if not frappe.db.exists("Payment Term", payment_term):
            frappe.throw(f"Payment Term '{payment_term}' does not exist in the system.")

        # Set allocate_payment_based_on_payment_terms to 1
        purchase_invoice.allocate_payment_based_on_payment_terms = 1

        # Clear existing payment schedule entries
        purchase_invoice.set("payment_schedule", [])

        # Calculate payment_amount
        payment_amount = purchase_invoice.grand_total  # Assuming full amount

        # Get due_date
        due_date =  purchase_invoice.due_date or payment_information.get("PaymentDueDate")

        # Create payment schedule entry
        payment_schedule_entry = {
            "doctype": "Payment Schedule",
            "payment_term": payment_term,
            "due_date": due_date,
            "invoice_portion": 100,
            "payment_amount": payment_amount,
        }
        purchase_invoice.append("payment_schedule", payment_schedule_entry)

        # Save the purchase_invoice with the new payment schedule
        purchase_invoice.save()

        # --- End code to handle PaymentMethod and Payment Schedule ---

        # Apply Purchase Taxes and Charges Template
        if purchase_invoice.taxes_and_charges:
            taxes_and_charges = get_taxes_and_charges(
                "Purchase Taxes and Charges Template",
                purchase_invoice.taxes_and_charges,
            )
            purchase_invoice.set("taxes", taxes_and_charges)
        
        #Link to Paperless Document
        paperless_doc_name = doc.get("paperless_doc")
        if paperless_doc_name:
            purchase_invoice.custom_paperless_document = paperless_doc_name

        purchase_invoice.save()

        # If paperless app is installed:
        if "frappe_goes_paperless" in frappe.get_installed_apps():
            # Attach link to paperless preview
            paperless_document_id = frappe.db.get_value(
                "Paperless Document",
                {"name": doc.get("paperless_doc")},
                "paperless_document_id",
            )
            if paperless_document_id:
                paperless_server, _ = get_paperless_settings()
                file_url = (
                    f"{paperless_server}/api/documents/{paperless_document_id}/preview/"
                )
                if not frappe.db.exists(
                    "File",
                    {
                        "file_url": file_url,
                        "attached_to_doctype": "Purchase Invoice",
                        "attached_to_name": purchase_invoice.name,
                    },
                ):
                    file_doc = frappe.new_doc("File")
                    file_doc.file_url = file_url
                    file_doc.file_name = "View Document"
                    file_doc.is_private = 1
                    file_doc.attached_to_doctype = "Purchase Invoice"
                    file_doc.attached_to_name = purchase_invoice.name
                    file_doc.save()

        # Update AI Query with the Purchase Invoice document
        ai_query_doc = frappe.get_doc("AI Query", doc.get("name"))
        ai_query_doc.document = purchase_invoice.name
        ai_query_doc.save()

        # Commit changes to the database
        frappe.db.commit()
        return purchase_invoice.name

    except KeyError as e:
        frappe.throw(f"Missing key in JSON data: {str(e)}")

    except Exception as e:
        frappe.throw(f"An unexpected error occurred: {str(e)}")




def create_or_get_item(
    supplier_item_code,
    item_name,
    item_description,
    supplier_name,
    item_group="All Item Groups",
    stock_uom="Stk",
):
    # Check if the supplier's item code is already linked to any existing item
    linked_item = frappe.db.get_value(
        "Item Supplier",
        {"supplier_part_no": supplier_item_code, "supplier": supplier_name},
        "parent",
    )

    if linked_item:
        # If it is linked, return the existing item
        print(
            f"Item with supplier's item code '{supplier_item_code}' already exists as '{linked_item}'."
        )
        return linked_item

    # If not linked, create a new item
    item = frappe.get_doc(
        {
            "doctype": "Item",
            "item_code": frappe.model.naming.make_autoname(
                "ITEM-.#####"
            ),  # Determine item_code by naming series
            "item_name": item_name,
            "description": item_description,
            "item_group": item_group,
            "stock_uom": stock_uom,
            "is_stock_item": 1,  # Set as a stock item
            "include_item_in_manufacturing": 0,
            "supplier_items": [
                {"supplier": supplier_name, "supplier_part_no": supplier_item_code}
            ],
        }
    )
    item.insert()
    frappe.db.commit()
    print(
        f"Item '{item.item_code}' created successfully with supplier's item code '{supplier_item_code}'!"
    )
    return item.name


def create_purchase_invoice_doc_item(item_code, qty, uom, price_list_rate):
    return frappe.get_doc(
        {
            "doctype": "Purchase Invoice Item",
            "item_code": item_code,
            "qty": qty,
            "uom": uom,
            "price_list_rate": price_list_rate,
        }
    )

def update_paperless_document_status(doc, method):
    """
    Updates the matching Paperless Document when a Purchase Invoice is submitted or canceled.
    """
    paperless_doc_name = doc.custom_paperless_document
    if not paperless_doc_name:
        frappe.msgprint("No Paperless Document linked to this Purchase Invoice.")
        return

    try:
        paperless_doc = frappe.get_doc("Paperless Document", paperless_doc_name)
    except frappe.DoesNotExistError:
        frappe.throw(f"Paperless Document '{paperless_doc_name}' not found.")
        return

    if method == "on_submit":
        # Update fields upon submission
        paperless_doc.status = "Workflow Successful"
        paperless_doc.frappe_doctype = "Purchase Invoice"
        paperless_doc.frappe_document = doc.name
    elif method == "on_cancel":
        # Update status upon cancellation
        paperless_doc.status = "Destination Document Cancelled"
        # Optionally clear fields
        # paperless_doc.frappe_doctype = ""
        # paperless_doc.frappe_document = ""
    else:
        # Handle other methods if necessary
        pass

    # Save changes
    paperless_doc.save(ignore_permissions=True)

def generate_item_number():
    # Generate a random combination of 10 digits and letters
    random_part = ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
    return f"AI-{random_part}"
