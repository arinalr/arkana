# Copyright 2019 Tecnativa - David Vidal
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).
from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare, float_is_zero, float_repr, format_datetime


class StockReturnRequest(models.Model):
    _name = "stock.return.request"
    _description = "Perform stock returns across pickings"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"

    name = fields.Char(
        "Reference",
        default=lambda self: self.env._("New"),
        copy=False,
        readonly=True,
        required=True,
    )
    partner_id = fields.Many2one(comodel_name="res.partner")
    return_type = fields.Selection(
        selection=[
            ("supplier", "Return to Supplier"),
            ("customer", "Return from Customer"),
            ("internal", "Return to Internal location"),
        ],
        required=True,
        help="Supplier - Search for incoming moves from this supplier\n"
        "Customer - Search for outgoing moves to this customer\n"
        "Internal - Search for outgoing moves to this location.",
    )
    return_from_location = fields.Many2one(
        comodel_name="stock.location",
        string="Return from",
        help="Return from this location",
        required=True,
        domain='[("usage", "=", "internal")]',
    )
    return_to_location = fields.Many2one(
        comodel_name="stock.location",
        string="Return to",
        help="Return to this location",
        required=True,
        domain='[("usage", "=", "internal")]',
    )
    return_order = fields.Selection(
        selection=[
            ("date desc, id desc", "Newer first"),
            ("date asc, id desc", "Older first"),
        ],
        default="date desc, id desc",
        required=True,
        help="The returns will be performed searching moves in the given order.",
    )
    from_date = fields.Date(
        string="Search moves up to this date",
    )
    picking_types = fields.Many2many(
        comodel_name="stock.picking.type",
        string="Operation types",
        help="Restrict operation types to search for",
    )
    state = fields.Selection(
        selection=[
            ("draft", "Open"),
            ("confirmed", "Confirmed"),
            ("done", "Done"),
            ("cancel", "Cancelled"),
        ],
        default="draft",
        readonly=True,
        copy=False,
        tracking=True,
    )
    returned_picking_ids = fields.One2many(
        comodel_name="stock.picking",
        inverse_name="stock_return_request_id",
        string="Returned Pickings",
        readonly=True,
        copy=False,
    )
    to_refund = fields.Boolean()
    show_to_refund = fields.Boolean(
        compute="_compute_show_to_refund",
        help="Whether to show it or not depending on the availability of"
        "the stock_account module (so a bridge module is not necessary)",
    )
    line_ids = fields.One2many(
        comodel_name="stock.return.request.line",
        inverse_name="request_id",
        string="Stock Return",
        copy=True,
    )
    note = fields.Text(
        string="Comments",
        help="They will be visible on the report",
    )

    @api.onchange("return_type", "partner_id")
    def onchange_locations(self):
        """UI helpers to determine locations"""
        warehouse = self._default_warehouse_id()
        if self.return_type == "supplier":
            self.return_to_location = self.partner_id.property_stock_supplier
            if self.return_from_location.usage != "internal":
                self.return_from_location = warehouse.lot_stock_id.id
        if self.return_type == "customer":
            self.return_from_location = self.partner_id.property_stock_customer
            if self.return_to_location.usage != "internal":
                self.return_to_location = warehouse.lot_stock_id.id
        if self.return_type == "internal":
            self.partner_id = False
            if self.return_to_location.usage != "internal":
                self.return_to_location = warehouse.lot_stock_id.id
            if self.return_from_location.usage != "internal":
                self.return_from_location = warehouse.lot_stock_id.id

    @api.model
    def _default_warehouse_id(self):
        warehouse = self.env["stock.warehouse"].search(
            [
                ("company_id", "=", self.env.company.id),
            ],
            limit=1,
        )
        return warehouse

    def _compute_show_to_refund(self):
        self.show_to_refund = "to_refund" in self.env["stock.move"]._fields

    def _prepare_return_picking(self, picking_dict, moves):
        """Extend to add more values if needed"""
        picking_type = self.env["stock.picking.type"].browse(
            picking_dict.get("picking_type_id")
        )
        return_picking_type = (
            picking_type.return_picking_type_id or picking_type.return_picking_type_id
        )
        picking_dict.update(
            {
                "move_ids": [(6, 0, moves.ids)],
                "move_line_ids": [(6, 0, moves.mapped("move_line_ids").ids)],
                "picking_type_id": return_picking_type.id,
                "state": "draft",
                "origin": self.env._("Return of %s", picking_dict.get("origin")),
                "location_id": self.return_from_location.id,
                "location_dest_id": self.return_to_location.id,
                "stock_return_request_id": self.id,
            }
        )
        return picking_dict

    def _create_picking(self, pickings, picking_moves):
        """Create return pickings with the proper moves"""
        return_pickings = self.env["stock.picking"]
        for picking in pickings:
            picking_dict = picking.copy_data(
                {
                    "origin": picking.name,
                    "printed": False,
                }
            )[0]
            moves = picking_moves.filtered(
                lambda x, picking=picking: x.origin_returned_move_id.picking_id
                == picking
            )
            new_picking = return_pickings.create(
                self._prepare_return_picking(picking_dict, moves)
            )
            new_picking.message_post_with_source(
                "mail.message_origin_link",
                render_values={"self": new_picking, "origin": picking},
                subtype_id=self.env.ref("mail.mt_note").id,
            )
            return_pickings += new_picking
        return return_pickings

    def _prepare_move_default_values(self, line, qty, move):
        """Extend this method to add values to return move"""
        vals = {
            "product_id": line.product_id.id,
            "product_uom_qty": qty,
            "product_uom": line.product_uom_id.id,
            "state": "draft",
            "location_id": line.request_id.return_from_location.id,
            "location_dest_id": line.request_id.return_to_location.id,
            "origin_returned_move_id": move.id,
            "procure_method": "make_to_stock",
            "picking_id": False,
        }
        if self.show_to_refund:
            vals["to_refund"] = line.request_id.to_refund
        return vals

    def _prepare_move_line_values(self, line, return_move, qty, quant=False):
        """Extend to add values to the move lines with lots"""
        vals = {
            "product_id": line.product_id.id,
            "product_uom_id": line.product_uom_id.id,
            "lot_id": line.lot_id.id,
            "location_id": return_move.location_id.id,
            "location_dest_id": return_move.location_dest_id._get_putaway_strategy(
                line.product_id
            ).id
            or return_move.location_dest_id.id,
            "quantity": qty,
        }
        if not quant:
            return vals
        if line.request_id.return_type in ["internal", "customer"]:
            vals["location_dest_id"] = quant.location_id.id
        else:
            vals["location_id"] = quant.location_id.id
        return vals

    def action_confirm(self):
        """Get moves and then try to reserve quantities. Fail if the quantites
        can't be assigned"""
        self.ensure_one()
        Quant = self.env["stock.quant"]
        if not self.line_ids:
            raise ValidationError(self.env._("Add some products to return"))
        returnable_moves = self.line_ids._get_returnable_move_ids()
        return_moves = self.env["stock.move"]
        failed_moves = []
        done_moves = {}
        for line in returnable_moves.keys():
            for qty, move in returnable_moves[line]:
                if move not in done_moves:
                    vals = self._prepare_move_default_values(line, qty, move)
                    return_move = move.copy(vals)
                else:
                    return_move = done_moves[move]
                    return_move.product_uom_qty += qty
                done_moves.setdefault(move, self.env["stock.move"])
                done_moves[move] += return_move
                if line.lot_id:
                    # We need to be deterministic with lots to avoid autoassign
                    # thus we create manually the line
                    return_move.with_context(skip_assign_move=True)._action_confirm()
                    # We try to reserve the stock manually so we ensure there's
                    # enough to make the return.
                    vals_list = []
                    if return_move.location_id.usage == "internal":
                        try:
                            quants = Quant._get_reserve_quantity(
                                line.product_id,
                                return_move.location_id,
                                qty,
                                lot_id=line.lot_id,
                                strict=False,
                            )
                            for q in quants:
                                vals_list.append(
                                    (
                                        0,
                                        0,
                                        self._prepare_move_line_values(
                                            line, return_move, q[1], q[0]
                                        ),
                                    )
                                )
                        except UserError:
                            failed_moves.append((line, return_move))
                    else:
                        vals_list.append(
                            (
                                0,
                                0,
                                self._prepare_move_line_values(line, return_move, qty),
                            )
                        )
                    return_move.write(
                        {
                            "move_line_ids": vals_list,
                        }
                    )
                    return_moves += return_move
                    line.returnable_move_ids += return_move
                # If not lots, just try standard assign
                else:
                    return_move._action_confirm()
                    # Force assign because the reservation method of picking type
                    # operation can be "manual" and the products would not be reserved
                    return_move._action_assign()
                    if return_move.state == "assigned":
                        return_move.quantity = qty
                        return_moves += return_move
                        line.returnable_move_ids += return_move
                    else:
                        failed_moves.append((line, return_move))
                        break
        if failed_moves:
            failed_moves_str = "\n".join(
                [
                    "{}: {} {:.2f}".format(
                        x[0].product_id.display_name,
                        x[0].lot_id.name or "\t",
                        x[0].quantity,
                    )
                    for x in failed_moves
                ]
            )
            raise ValidationError(
                self.env._(
                    "It wasn't possible to assign stock for this returns:\n"
                    "{failed_moves_str}"
                ).format(failed_moves_str=failed_moves_str)
            )
        # Finish move traceability
        for move in return_moves:
            vals = {}
            origin_move = move.origin_returned_move_id
            move_orig_to_link = origin_move.move_dest_ids.mapped("returned_move_ids")
            move_dest_to_link = origin_move.move_orig_ids.mapped("returned_move_ids")
            vals["move_orig_ids"] = [(4, m.id) for m in move_orig_to_link | origin_move]
            vals["move_dest_ids"] = [(4, m.id) for m in move_dest_to_link]
            move.write(vals)
        # Make return pickings and link to the proper moves.
        origin_pickings = return_moves.mapped("origin_returned_move_id.picking_id")
        self.returned_picking_ids = self._create_picking(origin_pickings, return_moves)
        self.state = "confirmed"

    def action_validate(self):
        # check availability again not only on action_confirm
        self.line_ids._get_returnable_move_ids()
        self.returned_picking_ids.move_ids.picked = True
        self.returned_picking_ids._action_done()
        self.state = "done"

    def action_cancel_to_draft(self):
        """Set to draft again"""
        self.filtered(lambda x: x.state == "cancel").write({"state": "draft"})

    def action_cancel(self):
        """Cancel request and the associated pickings. We can set it to
        draft again."""
        self.filtered(lambda x: x.state == "draft").write({"state": "cancel"})
        confirmed = self.filtered(lambda x: x.state == "confirmed")
        for return_request in confirmed:
            return_request.mapped("returned_picking_ids").action_cancel()
            return_request.state = "cancel"

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals["name"] == self.env._("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code(
                    "stock.return.request"
                ) or self.env._("New")
        return super().create(vals)

    @api.ondelete(at_uninstall=False)
    def _must_delete_request(self):
        for record in self:
            if record.state == "done":
                raise UserError(self.env._("You cannot delete this record."))

    def action_view_pickings(self):
        """Display returned pickings"""
        xmlid = "stock.action_picking_tree_incoming"
        if self.return_type == "customer":
            xmlid = "stock.action_picking_tree_outgoing"
        elif self.return_type == "internal":
            xmlid = "stock.action_picking_tree_internal"
        action = self.env["ir.actions.act_window"]._for_xml_id(xmlid)
        action["context"] = {}
        pickings = self.returned_picking_ids
        if not pickings or len(pickings) > 1:
            action["domain"] = f"[('id', 'in', {pickings.ids})]"
        elif len(pickings) == 1:
            res = self.env.ref("stock.view_picking_form", False)
            action["views"] = [(res and res.id or False, "form")]
            action["res_id"] = pickings.id
        return action

    def do_print_return_request(self):
        return self.env.ref(
            "stock_return_request.action_report_stock_return_request"
        ).report_action(self)


class StockReturnRequestLine(models.Model):
    _name = "stock.return.request.line"
    _description = "Product to search for returns"

    request_id = fields.Many2one(
        comodel_name="stock.return.request",
        string="Return Request",
        ondelete="cascade",
        required=True,
        readonly=True,
    )
    product_id = fields.Many2one(
        comodel_name="product.product",
        string="Product",
        required=True,
        domain=[("is_storable", "=", True)],
    )
    product_uom_id = fields.Many2one(
        comodel_name="uom.uom",
        related="product_id.uom_id",
        readonly=True,
    )
    tracking = fields.Selection(
        related="product_id.tracking",
        readonly=True,
    )
    lot_id = fields.Many2one(
        comodel_name="stock.lot",
        string="Lot / Serial",
        domain="[('product_id', '=', product_id)]",
    )
    quantity = fields.Float(
        string="Quantiy to return",
        digits="Product Unit of Measure",
        required=True,
    )
    max_quantity = fields.Float(
        string="Maximum available quantity",
        digits="Product Unit of Measure",
        compute="_compute_max_quantity",
    )
    returnable_move_ids = fields.Many2many(
        comodel_name="stock.move",
        string="Returnable Move Lines",
        copy=False,
        readonly=True,
    )

    def _get_moves_domain(self):
        """Domain constructor for moves search"""
        self.ensure_one()
        domain = [
            ("state", "=", "done"),
            ("origin_returned_move_id", "=", False),
            ("product_id", "=", self.product_id.id),
        ]
        if not self.env.context.get("ignore_rr_lots"):
            domain += [("move_line_ids.lot_id", "=", self.lot_id.id)]
        if self.request_id.from_date:
            domain += [("date", ">=", self.request_id.from_date)]
        if self.request_id.picking_types:
            domain += [
                ("picking_id.picking_type_id", "in", self.request_id.picking_types.ids)
            ]
        return_type = self.request_id.return_type
        if return_type != "internal":
            domain += [
                (
                    "picking_id.partner_id",
                    "child_of",
                    self.request_id.partner_id.commercial_partner_id.id,
                )
            ]
        # Search for movements coming delivered to that location
        if return_type in ["internal", "customer"]:
            domain += [
                ("location_dest_id", "=", self.request_id.return_from_location.id)
            ]
        # Return to supplier. Search for moves that came from that location
        else:
            domain += [
                ("location_id", "child_of", self.request_id.return_to_location.id)
            ]
        return domain

    def _get_returnable_move_ids(self):
        """Gets returnable stock.moves for the given request conditions

        :returns: a dict with request lines as keys containing a list of tuples
                  with qty returnable for a given move as the move itself
        :rtype: dictionary
        """
        moves_for_return = {}
        stock_move_obj = self.env["stock.move"]
        # Avoid lines with quantity to 0.0
        for line in self.filtered("quantity"):
            moves_for_return[line] = []
            precision = line.product_uom_id.rounding
            moves = stock_move_obj.search(
                line._get_moves_domain(), order=line.request_id.return_order
            )
            moves = moves.filtered(lambda m: m.qty_returnable > 0.0)
            # Add moves up to desired quantity
            qty_to_complete = line.quantity
            for move in moves:
                qty_returned = 0
                return_moves = move.returned_move_ids.filtered(
                    lambda x: x.state == "done"
                )
                # Don't count already returned
                if return_moves:
                    qty_returned = sum(
                        return_moves.mapped("move_line_ids")
                        .filtered(lambda x, line=line: x.lot_id == line.lot_id)
                        .mapped("quantity")
                    )
                quantity = sum(
                    move.mapped("move_line_ids")
                    .filtered(lambda x, line=line: x.lot_id == line.lot_id)
                    .mapped("quantity")
                )
                qty_remaining = quantity - qty_returned
                # We add the move to the list if there are units that haven't
                # been returned
                if float_compare(qty_remaining, 0.0, precision_rounding=precision) > 0:
                    qty_to_return = min(qty_to_complete, qty_remaining)
                    moves_for_return[line] += [(qty_to_return, move)]
                    qty_to_complete -= qty_to_return
                if float_is_zero(qty_to_complete, precision_rounding=precision):
                    break
            if qty_to_complete:
                qty_found = line.quantity - qty_to_complete
                raise ValidationError(
                    self.env._(
                        "Not enough moves to return this product.\n"
                        "It wasn't possible to find enough moves to return "
                        "{line_quantity} {line_product_uom_id_name} "
                        "of {line_product_id_displayname}. A maximum of {qty_found} "
                        "can be returned."
                    ).format(
                        line_quantity=line.quantity,
                        line_product_uom_id_name=line.product_uom_id.name,
                        line_product_id_displayname=line.product_id.display_name,
                        qty_found=qty_found,
                    )
                )
        return moves_for_return

    @api.model_create_multi
    def create(self, vals_list):
        new_records = super().create(vals_list)
        for record in new_records:
            existing = self.search_count(
                [
                    ("product_id", "=", record.product_id.id),
                    ("request_id.state", "in", ["draft", "confirm"]),
                    (
                        "request_id.return_from_location",
                        "=",
                        record.request_id.return_from_location.id,
                    ),
                    (
                        "request_id.return_to_location",
                        "=",
                        record.request_id.return_to_location.id,
                    ),
                    (
                        "request_id.partner_id",
                        "child_of",
                        record.request_id.partner_id.commercial_partner_id.id,
                    ),
                    ("lot_id", "=", record.lot_id.id),
                ]
            )
            if existing > 1:
                raise UserError(
                    self.env._(
                        "You cannot have two open Stock Return Requests with the same "
                        "product ({product_id}), locations ({return_from_location}, "
                        "{return_to_location}) partner ({partner_id}) and lot.\n"
                        "Please first validate the first return request with this "
                        "product before creating a new one."
                    ).format(
                        product_id=record.product_id.display_name,
                        return_from_location=record.request_id.return_from_location.display_name,
                        return_to_location=record.request_id.return_to_location.display_name,
                        partner_id=record.request_id.partner_id.name,
                    )
                )
        return new_records

    @api.depends("product_id", "lot_id")
    def _compute_max_quantity(self):
        self.max_quantity = 0
        for line in self.filtered(lambda x: x.request_id.return_type != "customer"):
            request = line.request_id
            search_args = [
                ("location_id", "child_of", request.return_from_location.id),
                ("product_id", "=", line.product_id.id),
            ]
            if line.lot_id:
                search_args.append(("lot_id", "=", line.lot_id.id))
            else:
                search_args.append(("lot_id", "=", False))
            res = self.env["stock.quant"].read_group(search_args, ["quantity"], [])
            line.max_quantity = res[0]["quantity"]

    def action_lot_suggestion(self):
        self.ensure_one()
        precision = self.env["decimal.precision"].precision_get(
            "Product Unit of Measure"
        )
        new_wizard = self.env["suggest.return.request.lot"].create(
            {"request_line_id": self.id}
        )
        moves = self.env["stock.move"].search(
            self.with_context(ignore_rr_lots=True)._get_moves_domain(),
            order=self.request_id.return_order,
        )
        suggested_lots_totals = {}
        suggested_lots_moves = {}
        vals_list = []
        for line in moves.move_line_ids:
            qty = line.move_id._get_lot_returnable_qty(line.lot_id)
            if float_compare(qty, 0, precision_digits=precision) > 0:
                suggested_lots_moves[line] = qty
                suggested_lots_totals.setdefault(line.lot_id, 0)
                suggested_lots_totals[line.lot_id] += qty
        for lot, qty in suggested_lots_totals.items():
            vals_list.append(
                {
                    "lot_id": lot.id,
                    "name": f"{lot.name} - {float_repr(qty, precision)}",
                    "lot_suggestion_mode": "sum",
                    "wizard_id": new_wizard.id,
                }
            )
        for move_line, qty in suggested_lots_moves.items():
            date_str = format_datetime(self.env, move_line.date, dt_format=None)
            name = (
                f"{date_str} - {move_line.lot_id.name} - "
                f"{move_line.reference} - {float_repr(qty, precision)}"
            )
            vals_list.append(
                {
                    "lot_id": move_line.lot_id.id,
                    "name": name,
                    "lot_suggestion_mode": "detail",
                    "wizard_id": new_wizard.id,
                }
            )
        if vals_list:
            self.env["suggest.return.request.lot.line"].create(vals_list)
        return {
            "name": self.env._("Suggest Lot"),
            "type": "ir.actions.act_window",
            "res_model": "suggest.return.request.lot",
            "view_mode": "form",
            "target": "new",
            "res_id": new_wizard.id,
        }
