# -*- encoding: utf-8 -*-
##############################################################################
#
#    Copyright (C) 2016 Compassion CH (http://www.compassion.ch)
#    Releasing children from poverty in Jesus' name
#    @author: Emanuel Cino <ecino@compassion.ch>
#
#    The licence is in the file __openerp__.py
#
##############################################################################
from enum import Enum

from datetime import datetime, timedelta

from ..mappings.child_reinstatement_mapping import ReinstatementMapping
from openerp import api, models, fields, _
from openerp.exceptions import Warning

from ..mappings.childpool_create_hold_mapping import ReservationToHoldMapping


class HoldType(Enum):
    """ Defines available Hold Types. """
    CHANGE_COMMITMENT_HOLD = 'Change Commitment Hold'
    CONSIGNMENT_HOLD = 'Consignment Hold'
    DELINQUENT_HOLD = 'Delinquent Mass Cancel Hold'
    E_COMMERCE_HOLD = 'E-Commerce Hold'
    NO_MONEY_HOLD = 'No Money Hold'
    REINSTATEMENT_HOLD = 'Reinstatement Hold'
    RESERVATION_HOLD = 'Reservation Hold'
    SPONSOR_CANCEL_HOLD = 'Sponsor Cancel Hold'
    SUB_CHILD_HOLD = 'Sub Child Hold'

    @staticmethod
    def get_hold_types():
        return [attr.value for attr in HoldType]

    @staticmethod
    def from_string(hold_type):
        """ Gets the HoldType given its string representation. """
        for etype in HoldType:
            if etype.value == hold_type:
                return etype
        return False


class AbstractHold(models.AbstractModel):
    """ Defines the basics of each model that must set up hold values. """
    _name = 'compassion.abstract.hold'

    ##########################################################################
    #                                 FIELDS                                 #
    ##########################################################################
    type = fields.Selection(
        'get_hold_types', required=True,
        default=HoldType.CONSIGNMENT_HOLD.value
    )
    expiration_date = fields.Datetime(required=True)
    primary_owner = fields.Many2one(
        'res.users', required=True, default=lambda self: self.env.user
    )
    secondary_owner = fields.Char()
    yield_rate = fields.Float()
    no_money_yield_rate = fields.Float()
    channel = fields.Selection('get_channel')
    source_code = fields.Char()
    comments = fields.Char()

    ##########################################################################
    #                             FIELDS METHODS                             #
    ##########################################################################
    @api.model
    def get_hold_types(self):
        return [(hold, hold) for hold in HoldType.get_hold_types()]

    @api.model
    def get_default_hold_expiration(self, hold_type):
        """
        Get the default hold expiration date.
        :param hold_type: HoldType Enum
        :return:
        """
        config_obj = self.env['availability.management.settings']
        hold_param = hold_type.name.lower() + '_duration'
        duration = config_obj.get_default_values([hold_param])[hold_param]
        diff = timedelta(days=duration) if hold_type !=  \
            HoldType.E_COMMERCE_HOLD else timedelta(minutes=duration)
        return fields.Datetime.to_string(datetime.now() + diff)

    @api.model
    def get_channel(self):
        return [
            ('web', _('Website')),
            ('event', _('Event')),
            ('ambassador', _('Ambassador')),
        ]

    @api.onchange('type')
    def onchange_type(self):
        self.expiration_date = self.get_default_hold_expiration(
            HoldType.from_string(self.type))

    ##########################################################################
    #                             PUBLIC METHODS                             #
    ##########################################################################
    def get_fields(self):
        """ Returns the fields for which we want to know the value. """
        return ['type', 'expiration_date', 'primary_owner',
                'secondary_owner', 'yield_rate', 'no_money_yield_rate',
                'channel', 'source_code', 'comments']

    def get_hold_values(self):
        """ Get the field values of one record.
            :return: Dictionary of values for the fields
        """
        self.ensure_one()
        vals = self.read(self.get_fields())[0]
        vals['primary_owner'] = vals['primary_owner'][0]
        del vals['id']
        return vals


class CompassionHold(models.Model):
    _name = 'compassion.hold'
    _rec_name = 'hold_id'
    _inherit = 'compassion.abstract.hold'

    hold_id = fields.Char(readonly=True)
    child_id = fields.Many2one(
        'compassion.child', 'Child on hold', readonly=True
    )
    child_name = fields.Char(
        'Child on hold', related='child_id.name', readonly=True
    )
    state = fields.Selection([
        ('draft', _("Draft")),
        ('active', _("Active")),
        ('expired', _("Expired"))],
        readonly=True, default='draft')
    reinstatement_reason = fields.Char(readonly=True)
    reservation_id = fields.Many2one('icp.reservation', 'Reservation')

    _sql_constraints = [
        ('hold_id', 'unique(hold_id)',
         'The hold already exists in database.'),
    ]

    ##########################################################################
    #                              ORM METHODS                               #
    ##########################################################################
    @api.multi
    def write(self, vals):
        res = super(CompassionHold, self).write(vals)
        notify_vals = ['name', 'primary_owner', 'type', 'mandatory_review',
                       'expiration_date']
        notify = reduce(lambda prev, val: prev or val in vals, notify_vals,
                        False)
        if notify and not self.env.context.get('no_upsert'):
            self.update_hold()

        return res

    @api.multi
    def unlink(self):
        self.release_hold()
        return

    ##########################################################################
    #                             PUBLIC METHODS                             #
    ##########################################################################
    def update_hold(self):
        message_obj = self.env['gmc.message.pool']
        action_id = self.env.ref('child_compassion.create_hold').id

        message_vals = {
            'action_id': action_id,
            'object_id': self.id
        }
        message_obj.create(message_vals).process_messages()

    @api.multi
    def hold_sent(self, vals):
        """ Called when hold is sent to Connect. """
        self.write(vals)
        # update compassion children with hold_id received
        for hold in self:
            child_to_update = hold.child_id
            if hold.hold_id:
                hold.state = 'active'
                child_vals = {
                    'hold_id': hold.id,
                    'active': True,
                    'state': 'N',
                }
                child_to_update.write(child_vals)
            else:
                # TODO Put hold in failure
                # delete child if no hold_id received
                child_to_update.unlink()
                hold.unlink()

    @api.model
    def reinstatement_notification(self, commkit_data):
        """ Called when a child was Reinstated. """
        hold_ids = list()
        reinstatement_mapping = ReinstatementMapping(self.env)

        for reinstatement_data in \
                commkit_data.get('BeneficiaryReinstatementNotificationList',
                                 [commkit_data]):
            vals = reinstatement_mapping.\
                get_vals_from_connect(reinstatement_data)
            hold = self.create(vals)

            # Update hold duration to what is configured
            hold.write({
                'expiration_date': self.get_default_hold_expiration(
                    HoldType.REINSTATEMENT_HOLD)
            })
            hold_ids.append(hold.id)

        return hold_ids

    def reservation_to_hold(self, commkit_data):
        """ Called when a reservation gots converted to a hold. """
        mapping = ReservationToHoldMapping(self.env)
        hold_data = commkit_data.get(
            'GlobalPartnerBeneficiaryReservationToHoldNotification')
        child_global_id = hold_data and hold_data.get('Beneficiary_GlobalID')
        if child_global_id:
            child = self.env['compassion.child'].create(
                {'global_id': child_global_id})
            hold = self.env['compassion.hold'].create(
                mapping.get_vals_from_connect(hold_data))
            if hold.reservation_id.source_code == 'sponsor_cancel':
                # Update the hold to sponsor cancel hold.
                hold.write({
                    'type': HoldType.SPONSOR_CANCEL_HOLD.value,
                    'expiration_date': self.get_default_hold_expiration(
                        HoldType.SPONSOR_CANCEL_HOLD)
                })
            child.hold_id = hold
            return [hold.id]

        return list()

    @api.multi
    def release_hold(self):
        message_obj = self.env['gmc.message.pool']
        action_id = self.env.ref('child_compassion.release_hold').id

        self.state = 'expired'
        message_vals = {
            'action_id': action_id,
            'object_id': self.id
        }

        if self.child_id.sponsor_id:
            raise Warning(_("Cancel impossible"), _("This hold is on a "
                                                    "sponsored child!"))
        else:
            self.child_id.active = False
            message_obj.create(message_vals)

    @api.model
    def check_hold_validity(self):
        expired_holds = self.env['compassion.hold'].search([
            ('expiration_date', '<',
             fields.Datetime.now())
        ])

        for expired_hold in expired_holds:
            expired_hold.child_id.active = False
            expired_hold.state = 'expired'

        return True
