class HomelabRouter:
    """The 'homelab' connection is used only for raw AgentCall logging inserts.
    Keep all ORM models and migrations on 'default'; never touch 'homelab'."""

    def db_for_read(self, model, **hints):
        return 'default'

    def db_for_write(self, model, **hints):
        return 'default'

    def allow_relation(self, obj1, obj2, **hints):
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        return db == 'default'
