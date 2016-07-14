from __future__ import absolute_import
from pony.py23compat import PY2, iteritems, basestring, unicode, buffer, int_types

import os
os.environ["NLS_LANG"] = "AMERICAN_AMERICA.UTF8"

from datetime import datetime, date, time, timedelta
from decimal import Decimal
from uuid import UUID

import cx_Oracle

from pony.orm import core, sqlbuilding, dbapiprovider, sqltranslation
from pony.orm.core import log_orm, log_sql, DatabaseError, TranslationError
from pony.orm.dbschema import DBSchema, DBObject, Table, Column
from pony.orm.ormtypes import Json, AnyItem
from pony.orm.dbapiprovider import DBAPIProvider, wrap_dbapi_exceptions, get_version_tuple
from pony.utils import throw
from pony.converting import timedelta2str

NoneType = type(None)

class OraTable(Table):
    def get_objects_to_create(table, created_tables=None):
        result = Table.get_objects_to_create(table, created_tables)
        for column in table.column_list:
            if column.is_pk == 'auto':
                sequence_name = column.converter.attr.kwargs.get('sequence_name')
                sequence = OraSequence(table, sequence_name)
                trigger = OraTrigger(table, column, sequence)
                result.extend((sequence, trigger))
                break
        return result

class OraSequence(DBObject):
    typename = 'Sequence'
    def __init__(sequence, table, name=None):
        sequence.table = table
        table_name = table.name
        if name is not None: sequence.name = name
        elif isinstance(table_name, basestring): sequence.name = table_name + '_SEQ'
        else: sequence.name = tuple(table_name[:-1]) + (table_name[0] + '_SEQ',)
    def exists(sequence, provider, connection, case_sensitive=True):
        if case_sensitive: sql = 'SELECT sequence_name FROM all_sequences ' \
                                 'WHERE sequence_owner = :so and sequence_name = :sn'
        else: sql = 'SELECT sequence_name FROM all_sequences ' \
                    'WHERE sequence_owner = :so and upper(sequence_name) = upper(:sn)'
        owner_name, sequence_name = provider.split_table_name(sequence.name)
        cursor = connection.cursor()
        cursor.execute(sql, dict(so=owner_name, sn=sequence_name))
        row = cursor.fetchone()
        return row[0] if row is not None else None
    def get_create_command(sequence):
        schema = sequence.table.schema
        seq_name = schema.provider.quote_name(sequence.name)
        return schema.case('CREATE SEQUENCE %s NOCACHE') % seq_name

trigger_template = """
CREATE TRIGGER %s
  BEFORE INSERT ON %s
  FOR EACH ROW
BEGIN
  IF :new.%s IS NULL THEN
    SELECT %s.nextval INTO :new.%s FROM DUAL;
  END IF;
END;""".strip()

class OraTrigger(DBObject):
    typename = 'Trigger'
    def __init__(trigger, table, column, sequence):
        trigger.table = table
        trigger.column = column
        trigger.sequence = sequence
        table_name = table.name
        if not isinstance(table_name, basestring): table_name = table_name[-1]
        trigger.name = table_name + '_BI' # Before Insert
    def exists(trigger, provider, connection, case_sensitive=True):
        if case_sensitive: sql = 'SELECT trigger_name FROM all_triggers ' \
                                 'WHERE table_name = :tbn AND table_owner = :o ' \
                                 'AND trigger_name = :trn AND owner = :o'
        else: sql = 'SELECT trigger_name FROM all_triggers ' \
                    'WHERE table_name = :tbn AND table_owner = :o ' \
                    'AND upper(trigger_name) = upper(:trn) AND owner = :o'
        owner_name, table_name = provider.split_table_name(trigger.table.name)
        cursor = connection.cursor()
        cursor.execute(sql, dict(tbn=table_name, trn=trigger.name, o=owner_name))
        row = cursor.fetchone()
        return row[0] if row is not None else None
    def get_create_command(trigger):
        schema = trigger.table.schema
        quote_name = schema.provider.quote_name
        trigger_name = quote_name(trigger.name)
        table_name = quote_name(trigger.table.name)
        column_name = quote_name(trigger.column.name)
        seq_name = quote_name(trigger.sequence.name)
        return schema.case(trigger_template) % (trigger_name, table_name, column_name, seq_name, column_name)

class OraColumn(Column):
    auto_template = None

class OraSchema(DBSchema):
    dialect = 'Oracle'
    table_class = OraTable
    column_class = OraColumn

class OraNoneMonad(sqltranslation.NoneMonad):
    def __init__(monad, translator, value=None):
        assert value in (None, '')
        sqltranslation.ConstMonad.__init__(monad, translator, None)

class OraConstMonad(sqltranslation.ConstMonad):
    @staticmethod
    def new(translator, value):
        if value == '': value = None
        return sqltranslation.ConstMonad.new(translator, value)

class OraTranslator(sqltranslation.SQLTranslator):
    dialect = 'Oracle'
    rowid_support = True
    NoneMonad = OraNoneMonad
    ConstMonad = OraConstMonad

    @classmethod
    def get_normalized_type_of(translator, value):
        if value == '': return NoneType
        return sqltranslation.SQLTranslator.get_normalized_type_of(value)

    class JsonItemMonad(sqltranslation.JsonItemMonad):
        def nonzero(monad):
            raise NotImplementedError

class OraBuilder(sqlbuilding.SQLBuilder):
    dialect = 'Oracle'
    def INSERT(builder, table_name, columns, values, returning=None):
        result = sqlbuilding.SQLBuilder.INSERT(builder, table_name, columns, values)
        if returning is not None:
            result.extend((' RETURNING ', builder.quote_name(returning), ' INTO :new_id'))
        return result
    def SELECT_FOR_UPDATE(builder, nowait, *sections):
        assert not builder.indent
        last_section = sections[-1]
        if last_section[0] != 'LIMIT':
            return builder.SELECT(*sections), 'FOR UPDATE NOWAIT\n' if nowait else 'FOR UPDATE\n'

        from_section = sections[1]
        assert from_section[0] == 'FROM'
        if len(from_section) > 2: throw(NotImplementedError,
            'Table joins are not supported for Oracle queries which have both FOR UPDATE and ROWNUM')

        order_by_section = None
        for section in sections:
            if section[0] == 'ORDER_BY': order_by_section = section

        table_ast = from_section[1]
        assert len(table_ast) == 3 and table_ast[1] == 'TABLE'
        table_alias = table_ast[0]
        rowid = [ 'COLUMN', table_alias, 'ROWID' ]
        sql_ast = [ 'SELECT', sections[0], [ 'FROM', table_ast ], [ 'WHERE', [ 'IN', rowid,
                    ('SELECT', [ 'ROWID', ['AS', rowid, 'row-id' ] ]) + sections[1:] ] ] ]
        if order_by_section: sql_ast.append(order_by_section)
        result = builder(sql_ast)
        return result, 'FOR UPDATE NOWAIT\n' if nowait else 'FOR UPDATE\n'
    def SELECT(builder, *sections):
        prev_suppress_aliases = builder.suppress_aliases
        builder.suppress_aliases = False
        try:
            last_section = sections[-1]
            limit = offset = None
            if last_section[0] == 'LIMIT':
                limit = last_section[1]
                if len(last_section) > 2: offset = last_section[2]
                sections = sections[:-1]
            result = builder.subquery(*sections)
            indent = builder.indent_spaces * builder.indent

            if sections[0][0] == 'ROWID':
                indent0 = builder.indent_spaces
                x = 't."row-id"'
            else:
                indent0 = ''
                x = 't.*'

            if not limit: pass
            elif not offset:
                result = [ indent0, 'SELECT * FROM (\n' ]
                builder.indent += 1
                result.extend(builder.subquery(*sections))
                builder.indent -= 1
                result.extend((indent, ') WHERE ROWNUM <= ', builder(limit), '\n'))
            else:
                indent2 = indent + builder.indent_spaces
                result = [ indent0, 'SELECT %s FROM (\n' % x, indent2, 'SELECT t.*, ROWNUM "row-num" FROM (\n' ]
                builder.indent += 2
                result.extend(builder.subquery(*sections))
                builder.indent -= 2
                result.extend((indent2, ') t '))
                if limit[0] == 'VALUE' and offset[0] == 'VALUE' \
                        and isinstance(limit[1], int) and isinstance(offset[1], int):
                    total_limit = [ 'VALUE', limit[1] + offset[1] ]
                    result.extend(('WHERE ROWNUM <= ', builder(total_limit), '\n'))
                else: result.extend(('WHERE ROWNUM <= ', builder(limit), ' + ', builder(offset), '\n'))
                result.extend((indent, ') t WHERE "row-num" > ', builder(offset), '\n'))
            if builder.indent:
                indent = builder.indent_spaces * builder.indent
                return '(\n', result, indent + ')'
            return result
        finally:
            builder.suppress_aliases = prev_suppress_aliases
    def ROWID(builder, *expr_list):
        return builder.ALL(*expr_list)
    def LIMIT(builder, limit, offset=None):
        assert False  # pragma: no cover
    def DATE(builder, expr):
        return 'TRUNC(', builder(expr), ')'
    def RANDOM(builder):
        return 'dbms_random.value'
    def DATE_ADD(builder, expr, delta):
        if isinstance(delta, timedelta):
            return '(', builder(expr), " + INTERVAL '", timedelta2str(delta), "' HOUR TO SECOND)"
        return '(', builder(expr), ' + ', builder(delta), ')'
    def DATE_SUB(builder, expr, delta):
        if isinstance(delta, timedelta):
            return '(', builder(expr), " - INTERVAL '", timedelta2str(delta), "' HOUR TO SECOND)"
        return '(', builder(expr), ' - ', builder(delta), ')'
    def DATETIME_ADD(builder, expr, delta):
        if isinstance(delta, timedelta):
            return '(', builder(expr), " + INTERVAL '", timedelta2str(delta), "' HOUR TO SECOND)"
        return '(', builder(expr), ' + ', builder(delta), ')'
    def DATETIME_SUB(builder, expr, delta):
        if isinstance(delta, timedelta):
            return '(', builder(expr), " - INTERVAL '", timedelta2str(delta), "' HOUR TO SECOND)"
        return '(', builder(expr), ' - ', builder(delta), ')'
    def JSON_GETPATH(builder, expr, path):
        for item in path:
            if isinstance(item, AnyItem):
                return 'JSON_QUERY(', builder(expr), ', ', builder.json_path(path), ' WITH WRAPPER)'
        query = 'JSON_QUERY(', builder(expr), ', ', builder.json_path(path), ' WITH WRAPPER)'
        return 'REGEXP_REPLACE(', query, ", '(^\\[|\\]$)', '')"
    def JSON_CONTAINS(builder, expr, path, key):
        assert key[0] == 'VALUE' and isinstance(key[1], basestring)
        expr_sql = builder(expr)
        path_sql = builder.json_path(path + [ key[1] ])
        return 'JSON_EXISTS(', expr_sql, ', ', path_sql, ')'

class OraBoolConverter(dbapiprovider.BoolConverter):
    if not PY2:
        def py2sql(converter, val):
            # Fixes cx_Oracle 5.1.3 Python 3 bug:
            # "DatabaseError: OCI-22062: invalid input string [True]"
            return int(val)
    def sql2py(converter, val):
        return bool(val)  # TODO: True/False, T/F, Y/N, Yes/No, etc.
    def sql_type(converter):
        return "NUMBER(1)"

class OraStrConverter(dbapiprovider.StrConverter):
    def validate(converter, val):
        if val == '': return None
        return dbapiprovider.StrConverter.validate(converter, val)
    def sql2py(converter, val):
        if isinstance(val, cx_Oracle.LOB):
            val = val.read()
            if PY2: val = val.decode('utf8')
        return val
    def sql_type(converter):
        # TODO: Add support for NVARCHAR2 and NCLOB datatypes
        if converter.max_len:
            return 'VARCHAR2(%d CHAR)' % converter.max_len
        return 'CLOB'

class OraIntConverter(dbapiprovider.IntConverter):
    signed_types = {None: 'NUMBER(38)', 8: 'NUMBER(3)', 16: 'NUMBER(5)', 24: 'NUMBER(7)', 32: 'NUMBER(10)', 64: 'NUMBER(19)'}
    unsigned_types = {None: 'NUMBER(38)', 8: 'NUMBER(3)', 16: 'NUMBER(5)', 24: 'NUMBER(8)', 32: 'NUMBER(10)', 64: 'NUMBER(20)'}
    def init(self, kwargs):
        dbapiprovider.IntConverter.init(self, kwargs)
        sequence_name = kwargs.pop('sequence_name', None)
        if sequence_name is not None and not (self.attr.auto and self.attr.is_pk):
            throw(TypeError, "Parameter 'sequence_name' can be used only for PrimaryKey attributes with auto=True")

class OraRealConverter(dbapiprovider.RealConverter):
    # Note that Oracle has differnet representation of float numbers
    def sql_type(converter):
        return 'NUMBER'

class OraDecimalConverter(dbapiprovider.DecimalConverter):
    def sql_type(converter):
        return 'NUMBER(%d, %d)' % (converter.precision, converter.scale)

class OraBlobConverter(dbapiprovider.BlobConverter):
    def sql2py(converter, val):
        return buffer(val.read())

class OraDateConverter(dbapiprovider.DateConverter):
    def sql2py(converter, val):
        if isinstance(val, datetime): return val.date()
        if not isinstance(val, date): throw(ValueError,
            'Value of unexpected type received from database: instead of date got %s', type(val))
        return val

class OraTimeConverter(dbapiprovider.TimeConverter):
    sql_type_name = 'INTERVAL DAY(0) TO SECOND'
    def __init__(converter, provider, py_type, attr=None):
        dbapiprovider.TimeConverter.__init__(converter, provider, py_type, attr)
        if attr is not None and converter.precision > 0:
            # cx_Oracle 5.1.3 corrupts microseconds for values of DAY TO SECOND type
            converter.precision = 0
    def sql2py(converter, val):
        if isinstance(val, timedelta):
            total_seconds = val.days * (24 * 60 * 60) + val.seconds
            if 0 <= total_seconds <= 24 * 60 * 60:
                minutes, seconds = divmod(total_seconds, 60)
                hours, minutes = divmod(minutes, 60)
                return time(hours, minutes, seconds, val.microseconds)
        elif not isinstance(val, time): throw(ValueError,
            'Value of unexpected type received from database%s: instead of time or timedelta got %s'
            % ('for attribute %s' % converter.attr if converter.attr else '', type(val)))
        return val
    def py2sql(converter, val):
        return timedelta(hours=val.hour, minutes=val.minute, seconds=val.second, microseconds=val.microsecond)

class OraTimedeltaConverter(dbapiprovider.TimedeltaConverter):
    sql_type_name = 'INTERVAL DAY TO SECOND'
    def __init__(converter, provider, py_type, attr=None):
        dbapiprovider.TimedeltaConverter.__init__(converter, provider, py_type, attr)
        if attr is not None and converter.precision > 0:
            # cx_Oracle 5.1.3 corrupts microseconds for values of DAY TO SECOND type
            converter.precision = 0

class OraDatetimeConverter(dbapiprovider.DatetimeConverter):
    sql_type_name = 'TIMESTAMP'

class OraUuidConverter(dbapiprovider.UuidConverter):
    def sql_type(converter):
        return 'RAW(16)'

class OraJsonConverter(dbapiprovider.JsonConverter):
    optimistic = False  # CLOBs cannot be compared with strings, and TO_CHAR(CLOB) returns first 4000 chars only
    def sql2py(converter, dbval):
        if hasattr(dbval, 'read'): dbval = dbval.read()
        return dbapiprovider.JsonConverter.sql2py(converter, dbval)
    def sql_type(converter):
        return 'CLOB'

class OraProvider(DBAPIProvider):
    dialect = 'Oracle'
    paramstyle = 'named'
    max_name_len = 30
    table_if_not_exists_syntax = False
    index_if_not_exists_syntax = False
    varchar_default_max_len = 1000
    uint64_support = True

    dbapi_module = cx_Oracle
    dbschema_cls = OraSchema
    translator_cls = OraTranslator
    sqlbuilder_cls = OraBuilder

    name_before_table = 'owner'

    converter_classes = [
        (NoneType, dbapiprovider.NoneConverter),
        (bool, OraBoolConverter),
        (basestring, OraStrConverter),
        (int_types, OraIntConverter),
        (float, OraRealConverter),
        (Decimal, OraDecimalConverter),
        (datetime, OraDatetimeConverter),
        (date, OraDateConverter),
        (time, OraTimeConverter),
        (timedelta, OraTimedeltaConverter),
        (UUID, OraUuidConverter),
        (buffer, OraBlobConverter),
        (Json, OraJsonConverter),
    ]

    @wrap_dbapi_exceptions
    def inspect_connection(provider, connection):
        cursor = connection.cursor()
        cursor.execute('SELECT version FROM product_component_version '
                       "WHERE product LIKE 'Oracle Database %'")
        provider.server_version = get_version_tuple(cursor.fetchone()[0])
        cursor.execute("SELECT sys_context( 'userenv', 'current_schema' ) FROM DUAL")
        provider.default_schema_name = cursor.fetchone()[0]

    def should_reconnect(provider, exc):
        reconnect_error_codes = (
            3113,  # ORA-03113: end-of-file on communication channel
            3114,  # ORA-03114: not connected to ORACLE
            )
        return isinstance(exc, cx_Oracle.OperationalError) \
               and exc.args[0].code in reconnect_error_codes

    def normalize_name(provider, name):
        return name[:provider.max_name_len].upper()

    def normalize_vars(provider, vars, vartypes):
        for name, value in iteritems(vars):
            if value == '':
                vars[name] = None
                vartypes[name] = NoneType

    @wrap_dbapi_exceptions
    def set_transaction_mode(provider, connection, cache):
        assert not cache.in_transaction
        db_session = cache.db_session
        if db_session is not None and db_session.serializable:
            cursor = connection.cursor()
            sql = 'SET TRANSACTION ISOLATION LEVEL SERIALIZABLE'
            if core.debug: log_orm(sql)
            cursor.execute(sql)
        cache.immediate = True
        if db_session is not None and (db_session.serializable or db_session.ddl):
            cache.in_transaction = True

    @wrap_dbapi_exceptions
    def execute(provider, cursor, sql, arguments=None, returning_id=False):
        if type(arguments) is list:
            assert arguments and not returning_id
            set_input_sizes(cursor, arguments[0])
            cursor.executemany(sql, arguments)
        else:
            if arguments is not None: set_input_sizes(cursor, arguments)
            if returning_id:
                var = cursor.var(cx_Oracle.STRING, 40, cursor.arraysize, outconverter=int)
                arguments['new_id'] = var
                if arguments is None: cursor.execute(sql)
                else: cursor.execute(sql, arguments)
                return var.getvalue()
            if arguments is None: cursor.execute(sql)
            else: cursor.execute(sql, arguments)

    def get_pool(provider, *args, **kwargs):
        user = password = dsn = None
        if len(args) == 1:
            conn_str = args[0]
            if '/' in conn_str:
                user, tail = conn_str.split('/', 1)
                if '@' in tail: password, dsn = tail.split('@', 1)
            if None in (user, password, dsn): throw(ValueError,
                "Incorrect connection string (must be in form of 'user/password@dsn')")
        elif len(args) == 2: user, password = args
        elif len(args) == 3: user, password, dsn = args
        elif args: throw(ValueError, 'Invalid number of positional arguments')
        if user != kwargs.setdefault('user', user):
            throw(ValueError, 'Ambiguous value for user')
        if password != kwargs.setdefault('password', password):
            throw(ValueError, 'Ambiguous value for password')
        if dsn != kwargs.setdefault('dsn', dsn):
            throw(ValueError, 'Ambiguous value for dsn')
        kwargs.setdefault('threaded', True)
        kwargs.setdefault('min', 1)
        kwargs.setdefault('max', 10)
        kwargs.setdefault('increment', 1)
        return OraPool(**kwargs)

    def table_exists(provider, connection, table_name, case_sensitive=True):
        owner_name, table_name = provider.split_table_name(table_name)
        cursor = connection.cursor()
        if case_sensitive: sql = 'SELECT table_name FROM all_tables WHERE owner = :o AND table_name = :tn'
        else: sql = 'SELECT table_name FROM all_tables WHERE owner = :o AND upper(table_name) = upper(:tn)'
        cursor.execute(sql, dict(o=owner_name, tn=table_name))
        row = cursor.fetchone()
        return row[0] if row is not None else None

    def index_exists(provider, connection, table_name, index_name, case_sensitive=True):
        owner_name, table_name = provider.split_table_name(table_name)
        if not isinstance(index_name, basestring): throw(NotImplementedError)
        if case_sensitive: sql = 'SELECT index_name FROM all_indexes WHERE owner = :o ' \
                                 'AND index_name = :i AND table_owner = :o AND table_name = :t'
        else: sql = 'SELECT index_name FROM all_indexes WHERE owner = :o ' \
                    'AND upper(index_name) = upper(:i) AND table_owner = :o AND table_name = :t'
        cursor = connection.cursor()
        cursor.execute(sql, dict(o=owner_name, i=index_name, t=table_name))
        row = cursor.fetchone()
        return row[0] if row is not None else None

    def fk_exists(provider, connection, table_name, fk_name, case_sensitive=True):
        owner_name, table_name = provider.split_table_name(table_name)
        if not isinstance(fk_name, basestring): throw(NotImplementedError)
        if case_sensitive:
            sql = "SELECT constraint_name FROM user_constraints WHERE constraint_type = 'R' " \
                  'AND table_name = :tn AND constraint_name = :cn AND owner = :o'
        else: sql = "SELECT constraint_name FROM user_constraints WHERE constraint_type = 'R' " \
                    'AND table_name = :tn AND upper(constraint_name) = upper(:cn) AND owner = :o'
        cursor = connection.cursor()
        cursor.execute(sql, dict(tn=table_name, cn=fk_name, o=owner_name))
        row = cursor.fetchone()
        return row[0] if row is not None else None

    def table_has_data(provider, connection, table_name):
        table_name = provider.quote_name(table_name)
        cursor = connection.cursor()
        cursor.execute('SELECT 1 FROM %s WHERE ROWNUM = 1' % table_name)
        return cursor.fetchone() is not None

    def drop_table(provider, connection, table_name):
        table_name = provider.quote_name(table_name)
        cursor = connection.cursor()
        sql = 'DROP TABLE %s CASCADE CONSTRAINTS' % table_name
        cursor.execute(sql)

provider_cls = OraProvider

def to_int_or_decimal(val):
    val = val.replace(',', '.')
    if '.' in val: return Decimal(val)
    return int(val)

def to_decimal(val):
    return Decimal(val.replace(',', '.'))

def output_type_handler(cursor, name, defaultType, size, precision, scale):
    if defaultType == cx_Oracle.NUMBER:
        if scale == 0:
            if precision: return cursor.var(cx_Oracle.STRING, 40, cursor.arraysize, outconverter=int)
            return cursor.var(cx_Oracle.STRING, 40, cursor.arraysize, outconverter=to_int_or_decimal)
        if scale != -127:
            return cursor.var(cx_Oracle.STRING, 100, cursor.arraysize, outconverter=to_decimal)
    elif defaultType in (cx_Oracle.STRING, cx_Oracle.FIXED_CHAR):
        return cursor.var(unicode, size, cursor.arraysize)  # from cx_Oracle example
    return None

class OraPool(object):
    forked_pools = []
    def __init__(pool, **kwargs):
        pool.kwargs = kwargs
        pool.cx_pool = cx_Oracle.SessionPool(**kwargs)
        pool.pid = os.getpid()
    def connect(pool):
        pid = os.getpid()
        if pool.pid != pid:
            pool.forked_pools.append((pool.cx_pool, pool.pid))
            pool.cx_pool = cx_Oracle.SessionPool(**pool.kwargs)
            pool.pid = os.getpid()
        if core.debug: log_orm('GET CONNECTION')
        con = pool.cx_pool.acquire()
        con.outputtypehandler = output_type_handler
        return con
    def release(pool, con):
        pool.cx_pool.release(con)
    def drop(pool, con):
        pool.cx_pool.drop(con)
    def disconnect(pool):
        pass

def get_inputsize(arg):
    if isinstance(arg, datetime):
        return cx_Oracle.TIMESTAMP
    return None

def set_input_sizes(cursor, arguments):
    if type(arguments) is dict:
        input_sizes = {}
        for name, arg in iteritems(arguments):
            size = get_inputsize(arg)
            if size is not None: input_sizes[name] = size
        cursor.setinputsizes(**input_sizes)
    elif type(arguments) is tuple:
        input_sizes = map(get_inputsize, arguments)
        cursor.setinputsizes(*input_sizes)
    else: assert False, type(arguments)  # pragma: no cover
