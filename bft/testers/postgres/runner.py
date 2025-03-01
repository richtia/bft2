import datetime
import os
from typing import Dict, NamedTuple

import psycopg

from bft.cases.runner import SqlCaseResult, SqlCaseRunner
from bft.cases.types import Case, CaseLiteral
from bft.dialects.types import SqlMapping

type_map = {
    "i16": "smallint",
    "i32": "integer",
    "i64": "bigint",
    "fp32": "float4",
    "fp64": "float8",
    "boolean": "boolean",
    "string": "text",
    "date": "date",
    "time": "time",
    "timestamp": "timestamp",
    "timestamp_tz": "timestamptz",
    "interval": "interval",
}


def type_to_postgres_type(type: str):
    if type not in type_map:
        return None
    return type_map[type]


def literal_to_str(lit: CaseLiteral):
    if lit.value is None:
        return "null"
    elif lit.value == float("inf"):
        return "'Infinity'"
    elif lit.value == float("-inf"):
        return "'-Infinity'"
    return str(lit.value)


def is_string_type(arg):
    return (
        arg.type in ["string", "timestamp", "timestamp_tz", "date", "time"]
        and arg.value is not None
    )


def is_datetype(arg):
    print(f"postgres type is: {type(arg)}")
    return type(arg) in [datetime.datetime, datetime.date, datetime.timedelta]


def get_connection_str():
    host = os.environ.get("POSTGRES_HOST", "localhost")
    dbname = os.environ.get("POSTGRES_DB", "bft")
    user = os.environ.get("POSTGRES_USER", "postgres")
    password = os.environ.get("POSTGRES_PASSWORD", "postgres")
    return f"{host=} {dbname=} {user=} {password=}"


class PostgresRunner(SqlCaseRunner):
    def __init__(self, dialect):
        super().__init__(dialect)
        self.conn = psycopg.connect(get_connection_str())

    def run_sql_case(self, case: Case, mapping: SqlMapping) -> SqlCaseResult:
        self.conn.execute("BEGIN;")

        try:
            arg_defs = []
            for idx, arg in enumerate(case.args):
                arg_type = type_to_postgres_type(arg.type)
                if arg_type is None:
                    return SqlCaseResult.unsupported(f"Unsupported type {arg.type}")
                arg_defs.append(f"arg{idx} {arg_type}")
            schema = ",".join(arg_defs)
            self.conn.execute(f"CREATE TABLE my_table({schema});")

            arg_names = [f"arg{idx}" for idx in range(len(case.args))]
            if mapping.aggregate:
                arg_names = [arg_names[0]]
            joined_arg_names = ",".join(arg_names)

            arg_vals_list = list()
            for arg in case.args:
                if is_string_type(arg):
                    arg_vals_list.append("'" + literal_to_str(arg) + "'")
                else:
                    arg_vals_list.append(literal_to_str(arg))
            arg_vals = ", ".join(arg_vals_list)
            if mapping.aggregate:
                arg_vals = ",".join([literal_to_str(arg) for arg in case.args])
                arg_vals_list = ", ".join(f"({val})" for val in arg_vals.split(","))
                if arg_vals != "[]":
                    self.conn.execute(
                        f"INSERT INTO my_table ({joined_arg_names}) VALUES {arg_vals_list};"
                    )
            else:
                self.conn.execute(
                    f"INSERT INTO my_table ({joined_arg_names}) VALUES ({arg_vals});"
                )

            if mapping.infix:
                if len(arg_names) != 2:
                    raise Exception(f"Infix function with {len(arg_names)} args")
                expr = f"SELECT {arg_names[0]} {mapping.local_name} {arg_names[1]} FROM my_table;"
            elif mapping.postfix:
                if len(arg_names) != 1:
                    raise Exception(f"Postfix function with {len(arg_names)} args")
                expr = f"SELECT {arg_names[0]} {mapping.local_name} FROM my_table;"
            elif mapping.extract:
                if len(arg_names) != 2:
                    raise Exception(f"Extract function with {len(arg_names)} args")
                expr = f"SELECT {mapping.local_name}({arg_vals_list[0]} FROM {arg_names[1]}) FROM my_table;"
            elif mapping.between:
                if len(arg_names) != 3:
                    raise Exception(f"Between function with {len(arg_names)} args")
                expr = f"SELECT {arg_names[0]} BETWEEN {arg_names[1]} AND {arg_names[2]} FROM my_table;"
            elif mapping.aggregate:
                if len(arg_names) < 1:
                    raise Exception(f"Aggregate function with {len(arg_names)} args")
                expr = f"SELECT {mapping.local_name}({arg_names[0]}) FROM my_table;"
            else:
                expr = f"SELECT {mapping.local_name}({joined_arg_names}) FROM my_table;"
            result = self.conn.execute(expr).fetchone()[0]

            if case.result == "undefined":
                return SqlCaseResult.success()
            elif case.result == "error":
                return SqlCaseResult.unexpected_pass(str(result))
            elif case.result == "nan":
                print(f"Expected NAN but received {result}")
                return SqlCaseResult.error(str(result))
            else:
                if result == case.result.value:
                    return SqlCaseResult.success()
                elif is_datetype(result) and str(result) == case.result.value:
                    return SqlCaseResult.success()
                else:
                    return SqlCaseResult.mismatch(str(result))
        except psycopg.Error as err:
            return SqlCaseResult.error(str(err))
        finally:
            self.conn.rollback()
