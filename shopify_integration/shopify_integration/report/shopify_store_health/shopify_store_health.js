frappe.query_reports["Shopify Store Health"] = {
	filters: [
		{
			fieldname: "days",
			label: __("Days"),
			fieldtype: "Int",
			default: 7,
		},
	],
};
