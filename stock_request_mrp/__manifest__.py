# Copyright 2017-24 ForgeFlow S.L. (https://www.forgeflow.com)
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

{
    "name": "Stock Request MRP",
    "summary": "Manufacturing request for stock",
    "version": "18.0.1.0.0",
    "license": "LGPL-3",
    "website": "https://github.com/OCA/stock-logistics-request",
    "author": "ForgeFlow, Odoo Community Association (OCA)",
    "maintainers": ["LoisRForgeFlow", "etobella"],
    "category": "Warehouse Management",
    "depends": ["stock_request", "mrp"],
    "data": [
        "security/ir.model.access.csv",
        "views/stock_request_views.xml",
        "views/stock_request_order_views.xml",
        "views/mrp_production_views.xml",
    ],
    "installable": True,
    "auto_install": True,
    "post_init_hook": "post_init_hook",
}
