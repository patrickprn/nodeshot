from django.conf import settings

PUBLISHED_DEFAULT = getattr(settings, 'NODESHOT_NODES_PUBLISHED_DEFAULT', True)
HSTORE_SCHEMA = getattr(settings, 'NODESHOT_NODES_HSTORE_SCHEMA', None)
REVERSION_ENABLED = getattr(settings, 'NODESHOT_NODES_REVERSION_ENABLED', True)
