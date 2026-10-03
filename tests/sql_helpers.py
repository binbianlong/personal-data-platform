class TracedConnection:
    """Record database requests while executing them against the real connection."""

    def __init__(self, connection):
        self.connection = connection
        self.statements = []

    def execute(self, sql, parameters=None):
        self.statements.append(" ".join(sql.split()))
        if parameters is None:
            return self.connection.execute(sql)
        return self.connection.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)
