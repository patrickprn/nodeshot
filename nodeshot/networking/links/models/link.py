from django.contrib.gis.db import models
from django.contrib.gis.geos import LineString
from django.utils.translation import ugettext_lazy as _
from django.core.exceptions import ValidationError
from django.db.models import Q

from django_hstore.fields import DictionaryField, ReferencesField

from netaddr import valid_ipv4, valid_ipv6, valid_mac

from nodeshot.core.base.managers import HStoreGeoAccessLevelManager as LinkManager
from nodeshot.core.base.models import BaseAccessLevel
from nodeshot.core.base.utils import choicify

from nodeshot.core.nodes.models import Node
from nodeshot.core.layers.models import Layer
from nodeshot.networking.net.models import Interface, Ip
from nodeshot.networking.net.models.choices import INTERFACE_TYPES

from .choices import METRIC_TYPES, LINK_STATUS, LINK_TYPES
from .topology import Topology
from ..exceptions import LinkDataNotFound, LinkNotFound


class Link(BaseAccessLevel):
    """
    Link Model
    Designed for both wireless and wired links
    """
    type = models.SmallIntegerField(_('type'), max_length=10, null=True, blank=True,
                                    choices=choicify(LINK_TYPES), default=LINK_TYPES.get('radio'))

    # in most cases these two fields are mandatory, except for "planned" links
    interface_a = models.ForeignKey(Interface, verbose_name=_('from interface'),
                                    related_name='link_interface_from', blank=True, null=True,
                                    help_text=_('mandatory except for "planned" links (in planned links you might not have any device installed yet)'))
    interface_b = models.ForeignKey(Interface, verbose_name=_('to interface'),
                                    related_name='link_interface_to', blank=True, null=True,
                                    help_text=_('mandatory except for "planned" links (in planned links you might not have any device installed yet)'))

    topology = models.ForeignKey(Topology, blank=True, null=True,
                                 help_text=_('mandatory to draw the link dinamically'))

    # in "planned" links these two fields are necessary
    # while in all the other status they serve as a shortcut
    node_a = models.ForeignKey(Node, verbose_name=_('from node'),
                               related_name='link_node_from', blank=True, null=True,
                               help_text=_('leave blank (except for planned nodes) as it will be filled in automatically'))
    node_b = models.ForeignKey(Node, verbose_name=_('to node'),
                               related_name='link_node_to', blank=True, null=True,
                               help_text=_('leave blank (except for planned nodes) as it will be filled in automatically'))
    # shortcut
    layer = models.ForeignKey(Layer, verbose_name=_('layer'), blank=True, null=True,
                              help_text=_('leave blank - it will be filled in automatically'))

    # geospatial info
    line = models.LineStringField(blank=True, null=True, help_text=_('leave blank and the line will be drawn automatically'))

    # monitoring info
    status = models.SmallIntegerField(_('status'), choices=choicify(LINK_STATUS), default=LINK_STATUS.get('planned'))
    first_seen = models.DateTimeField(_('first time seen on'), blank=True, null=True, default=None)
    last_seen = models.DateTimeField(_('last time seen on'), blank=True, null=True, default=None)

    # technical info
    metric_type = models.CharField(_('metric type'), max_length=6,
                                   choices=choicify(METRIC_TYPES), blank=True, null=True)
    metric_value = models.FloatField(_('metric value'), blank=True, null=True)
    max_rate = models.IntegerField(_('Maximum BPS'), null=True, default=None, blank=True)
    min_rate = models.IntegerField(_('Minimum BPS'), null=True, default=None, blank=True)

    # wireless specific info
    dbm = models.IntegerField(_('dBm average'), null=True, default=None, blank=True)
    noise = models.IntegerField(_('noise average'), null=True, default=None, blank=True)

    # additional data
    data = DictionaryField(_('extra data'), null=True, blank=True,
                           help_text=_('store extra attributes in JSON string'))
    shortcuts = ReferencesField(null=True, blank=True)

    # django manager
    objects = LinkManager()

    class Meta:
        app_label = 'links'

    def __unicode__(self):
        return _(u'%s <> %s') % (self.node_a_name, self.node_b_name)

    def clean(self, *args, **kwargs):
        """
        Custom validation
            1. interface_a and interface_b mandatory except for planned links
            2. planned links should have at least node_a and node_b filled in
            3. dbm and noise fields can be filled only for radio links
            4. interface_a and interface_b must differ
            5. interface a and b type must match
        """
        if self.status != LINK_STATUS.get('planned'):
            if self.interface_a is None or self.interface_b is None:
                raise ValidationError(_('fields "from interface" and "to interface" are mandatory in this case'))

            if (self.interface_a_id == self.interface_b_id) or (self.interface_a == self.interface_b):
                raise ValidationError(_('link cannot have same "from interface" and "to interface"'))

        if self.status == LINK_STATUS.get('planned') and (self.node_a is None or self.node_b is None):
            raise ValidationError(_('fields "from node" and "to node" are mandatory for planned links'))

        if self.type != LINK_TYPES.get('radio') and (self.dbm is not None or self.noise is not None):
            raise ValidationError(_('Only links of type "radio" can contain "dbm" and "noise" information'))

        if (self.interface_a and self.interface_b) and self.interface_a.type != self.interface_b.type:
            format_tuple = (self.interface_a.get_type_display(), self.interface_b.get_type_display())
            raise ValidationError(_('link cannot be between of interfaces of different types:\
                                    interface a is "%s" while b is "%s"') % format_tuple)

    def save(self, *args, **kwargs):
        """
        Custom save does the following:
            * determine link type if not specified
            * automatically fill 'node_a' and 'node_b' fields if necessary
            * draw line between two nodes
            * fill shortcut properties node_a_name and node_b_name
        """
        if not self.type:
            if self.interface_a.type == INTERFACE_TYPES.get('wireless'):
                self.type = LINK_TYPES.get('radio')
            elif self.interface_a.type == INTERFACE_TYPES.get('ethernet'):
                self.type = LINK_TYPES.get('ethernet')
            else:
                self.type = LINK_TYPES.get('virtual')

        if self.interface_a_id:
            self.interface_a = Interface.objects.get(pk=self.interface_a_id)
        if self.interface_b_id:
            self.interface_b = Interface.objects.get(pk=self.interface_b_id)

        # fill in node_a and node_b
        if self.node_a is None and self.interface_a is not None:
            self.node_a = self.interface_a.node
        if self.node_b is None and self.interface_b is not None:
            self.node_b = self.interface_b.node

        # fill layer from node_a
        if self.layer is None:
            self.layer = self.node_a.layer

        # draw linestring
        if not self.line:
            self.line = LineString(self.node_a.point, self.node_b.point)

        # fill properties
        if self.data.get('node_a_name', None) is None:
            self.data['node_a_name'] = self.node_a.name
            self.data['node_b_name'] = self.node_b.name

        if self.data.get('node_a_slug', None) is None or self.data.get('node_b_slug', None) is None:
            self.data['node_a_slug'] = self.node_a.slug
            self.data['node_b_slug'] = self.node_b.slug

        if self.interface_a and self.data.get('interface_a_mac', None) is None:
            self.data['interface_a_mac'] = self.interface_a.mac

        if self.interface_b and self.data.get('interface_b_mac', None) is None:
            self.data['interface_b_mac'] = self.interface_b.mac

        if self.data.get('layer_slug') != self.layer.slug:
            self.data['layer_slug'] = self.layer.slug

        super(Link, self).save(*args, **kwargs)

    @classmethod
    def find_from_tuple(cls, link):
        """
        Find link by providing a tuple with two ip addresses or two mac addresses
        :param link: tuple with two string elements indicating source and destination (ip or mac addresses)
        :returns: Link object
        """
        try:
            a = link[0]
            b = link[1]
        except IndexError:
            raise ValueError('Expecting tuple with source and destination')
        # find interfaces
        if (valid_ipv4(a) and valid_ipv4(b)) or (valid_ipv6(a) and valid_ipv6(b)):
            try:
                a = Ip.objects.get(address=a).interface
                b = Ip.objects.get(address=b).interface
            except Ip.DoesNotExist as e:
                raise LinkDataNotFound(e)
        elif valid_mac(a) and valid_mac(b):
            try:
                a = Interface.objects.get(mac=a)
                b = Interface.objects.get(mac=b)
            except Interface.DoesNotExist as e:
                raise LinkDataNotFound(e)
        else:
            raise ValueError('Expecting valid ipv4, ipv6 or mac address')
        # find link with interfaces
        # inverse order is also ok
        q = Q(interface_a=a, interface_b=b) | Q(interface_a=b, interface_b=a)
        link = Link.objects.filter(q).first()
        if link is None:
            raise LinkNotFound('Link matching query does not exist',
                               interface_a=a,
                               interface_b=b)
        return link

    @classmethod
    def find_or_create(cls, link):
        """
        Same as `find_from_tuple` but creates the link if it does not exist
        """
        try:
            return cls.find_from_tuple(link)
        except LinkNotFound as e:
            pass
        # create link
        link = Link(interface_a=e.interface_a,
                    interface_b=e.interface_b,
                    status=LINK_STATUS.get('active'))
        link.full_clean()
        link.save()
        return link

    @property
    def node_a_name(self):
        return self.data.get('node_a_name', None)

    @property
    def node_b_name(self):
        return self.data.get('node_b_name', None)

    @property
    def node_a_slug(self):
        return self.data.get('node_a_slug', None)

    @property
    def node_b_slug(self):
        return self.data.get('node_b_slug', None)

    @property
    def interface_a_mac(self):
        return self.data.get('interface_a_mac', None)

    @property
    def interface_b_mac(self):
        return self.data.get('interface_b_mac', None)

    @property
    def layer_slug(self):
        return self.data.get('layer_slug', None)

    @property
    def quality(self):
        """
        Quality is a number between 1 and 6 that rates the quality of the link.
        The way quality is calculated might be overridden by settings.
        0 means unknown
        """
        if self.metric_value is None:
            return 0
        # PLACEHOLDER
        return 6
