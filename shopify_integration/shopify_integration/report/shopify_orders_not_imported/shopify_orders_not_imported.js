frappe.query_reports["Shopify Orders Not Imported"] = {
	filters: [
		{
			fieldname: "store",
			label: __("Store"),
			fieldtype: "Link",
			options: "Shopify Store",
		},
		{
			fieldname: "status",
			label: __("Status"),
			fieldtype: "Select",
			options: "\nPending\nSkipped\nFailed\nResolved",
		},
	],
};
