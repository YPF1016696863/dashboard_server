import logging
import os

from redash.query_runner import *
from redash.settings import parse_boolean
from redash.utils import json_dumps, json_loads

logger = logging.getLogger(__name__)
types_map = {
    0: TYPE_FLOAT,
    1: TYPE_INTEGER,
    2: TYPE_INTEGER,
    3: TYPE_INTEGER,
    4: TYPE_FLOAT,
    5: TYPE_FLOAT,
    7: TYPE_DATETIME,
    8: TYPE_INTEGER,
    9: TYPE_INTEGER,
    10: TYPE_DATE,
    12: TYPE_DATETIME,
    15: TYPE_STRING,
    16: TYPE_INTEGER,
    246: TYPE_FLOAT,
    253: TYPE_STRING,
    254: TYPE_STRING,
}


class Mysql(BaseSQLQueryRunner):
    noop_query = "SELECT 1"

    @classmethod
    def configuration_schema(cls):
        show_ssl_settings = parse_boolean(os.environ.get('MYSQL_SHOW_SSL_SETTINGS', 'true'))

        schema = {
            'type': 'object',
            'properties': {
                'host': {
                    'type': 'string',
                    'default': '127.0.0.1',
                    'title': 'Host'
                },
                'user': {
                    'type': 'string',
                    'title': 'User'
                },
                'passwd': {
                    'type': 'string',
                    'title': 'Password'
                },
                'db': {
                    'type': 'string',
                    'title': 'Database Name'
                },
                'port': {
                    'type': 'number',
                    'default': 3306,
                    'title': 'Port'
                }
            },
            "order": ['host', 'port', 'user', 'passwd', 'db'],
            'required': ['db'],
            'secret': ['passwd']
        }

        if show_ssl_settings:
            schema['properties'].update({
                'use_ssl': {
                    'type': 'boolean',
                    'title': 'Use SSL'
                },
                'ssl_cacert': {
                    'type': 'string',
                    'title': 'Path to SSL Cacert'
                },
                'ssl_cert': {
                    'type': 'string',
                    'title': 'Path to SSL Cert'
                },
                'ssl_key': {
                    'type': 'string',
                    'title': 'Path to SSL Key'
                }
            })

        return schema

    @classmethod
    def name(cls):
        return "MySQL Database"

    @classmethod
    def enabled(cls):
        try:
            import MySQLdb
        except ImportError:
            return False

        return True

    def _get_tables(self, schema):
        query = """
        SELECT col.table_schema as table_schema,
               col.table_name as table_name,
               col.column_name as column_name
        FROM `information_schema`.`columns` col
        WHERE col.table_schema NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys');
        """

        results, error = self.run_query(query, None)

        if error is not None:
            raise Exception("Failed getting schema.")

        results = json_loads(results)

        for row in results['rows']:
            if row['table_schema'] != self.configuration['db']:
                table_name = u'{}.{}'.format(row['table_schema'], row['table_name'])
            else:
                table_name = row['table_name']

            if table_name not in schema:
                schema[table_name] = {'name': table_name, 'columns': []}

            schema[table_name]['columns'].append(row['column_name'])

        return schema.values()

    def run_query(self, query, user):
        import MySQLdb

        connection = None
        try:
            connection = MySQLdb.connect(host=self.configuration.get('host', ''),
                                         user=self.configuration.get('user', ''),
                                         passwd=self.configuration.get('passwd', ''),
                                         db=self.configuration['db'],
                                         port=self.configuration.get('port', 3306),
                                         charset='utf8', use_unicode=True,
                                         ssl=self._get_ssl_parameters(),
                                         connect_timeout=60)
            cursor = connection.cursor()
            logger.debug("MySQL running query: %s", query)
            cursor.execute(query)

            data = cursor.fetchall()

            while cursor.nextset():
                data = cursor.fetchall()

            # TODO - very similar to pg.py
            if cursor.description is not None:
                columns = self.fetch_columns([(i[0], types_map.get(i[1], None)) for i in cursor.description])
                rows = [dict(zip((c['name'] for c in columns), row)) for row in data]

                data = {'columns': columns, 'rows': rows}
                json_data = json_dumps(data)
                error = None
            else:
                json_data = None
                error = "No data was returned."

            cursor.close()
        except MySQLdb.Error as e:
            json_data = None
            error = e.args[1]
        except KeyboardInterrupt:
            cursor.close()
            error = "Query cancelled by user."
            json_data = None
        finally:
            if connection:
                connection.close()

        return json_data, error

    def _get_ssl_parameters(self):
        ssl_params = {}

        if self.configuration.get('use_ssl'):
            config_map = dict(ssl_cacert='ca',
                              ssl_cert='cert',
                              ssl_key='key')
            for key, cfg in config_map.items():
                val = self.configuration.get(key)
                if val:
                    ssl_params[cfg] = val

        return ssl_params


class RDSMySQL(Mysql):
    @classmethod
    def name(cls):
        return "MySQL (Amazon RDS)"

    @classmethod
    def type(cls):
        return 'rds_mysql'

    @classmethod
    def configuration_schema(cls):
        return {
            'type': 'object',
            'properties': {
                'host': {
                    'type': 'string',
                },
                'user': {
                    'type': 'string'
                },
                'passwd': {
                    'type': 'string',
                    'title': 'Password'
                },
                'db': {
                    'type': 'string',
                    'title': 'Database name'
                },
                'port': {
                    'type': 'number',
                    'default': 3306,
                },
                'use_ssl': {
                    'type': 'boolean',
                    'title': 'Use SSL'
                }
            },
            "order": ['host', 'port', 'user', 'passwd', 'db'],
            'required': ['db', 'user', 'passwd', 'host'],
            'secret': ['passwd']
        }

    def _get_ssl_parameters(self):
        if self.configuration.get('use_ssl'):
            ca_path = os.path.join(os.path.dirname(__file__), './files/rds-combined-ca-bundle.pem')
            return {'ca': ca_path}

        return {}


register(Mysql)
# register(RDSMySQL)
