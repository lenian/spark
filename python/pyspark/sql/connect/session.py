#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import os
import warnings
from distutils.version import LooseVersion
from threading import RLock
from collections.abc import Sized
from functools import reduce

import numpy as np
import pandas as pd
import pyarrow as pa

from pyspark import SparkContext, SparkConf, __version__
from pyspark.java_gateway import launch_gateway
from pyspark.sql.session import classproperty, SparkSession as PySparkSession
from pyspark.sql.types import (
    _infer_schema,
    _has_nulltype,
    _merge_type,
    Row,
    DataType,
    StructType,
    AtomicType,
)
from pyspark.sql.utils import to_str

from pyspark.sql.connect.client import SparkConnectClient
from pyspark.sql.connect.dataframe import DataFrame
from pyspark.sql.connect.plan import SQL, Range, LocalRelation
from pyspark.sql.connect.readwriter import DataFrameReader

from typing import (
    Optional,
    Any,
    Union,
    Dict,
    List,
    Tuple,
    cast,
    overload,
    Iterable,
    TYPE_CHECKING,
)


if TYPE_CHECKING:
    from pyspark.sql.connect._typing import OptionalPrimitiveType
    from pyspark.sql.connect.catalog import Catalog


class SparkSession:
    class Builder:
        """Builder for :class:`SparkSession`."""

        _lock = RLock()

        def __init__(self) -> None:
            self._options: Dict[str, Any] = {}

        @overload
        def config(self, key: str, value: Any) -> "SparkSession.Builder":
            ...

        @overload
        def config(self, *, map: Dict[str, "OptionalPrimitiveType"]) -> "SparkSession.Builder":
            ...

        def config(
            self,
            key: Optional[str] = None,
            value: Optional[Any] = None,
            *,
            map: Optional[Dict[str, "OptionalPrimitiveType"]] = None,
        ) -> "SparkSession.Builder":
            with self._lock:
                if map is not None:
                    for k, v in map.items():
                        self._options[k] = to_str(v)
                else:
                    self._options[cast(str, key)] = to_str(value)
                return self

        def master(self, master: str) -> "SparkSession.Builder":
            return self

        def appName(self, name: str) -> "SparkSession.Builder":
            return self.config("spark.app.name", name)

        def remote(self, location: str = "sc://localhost") -> "SparkSession.Builder":
            return self.config("spark.remote", location)

        def enableHiveSupport(self) -> "SparkSession.Builder":
            raise NotImplementedError("enableHiveSupport not implemented for Spark Connect")

        def getOrCreate(self) -> "SparkSession":
            return SparkSession(connectionString=self._options["spark.remote"])

    _client: SparkConnectClient

    @classproperty
    def builder(cls) -> Builder:
        """Creates a :class:`Builder` for constructing a :class:`SparkSession`."""
        return cls.Builder()

    def __init__(self, connectionString: str, userId: Optional[str] = None):
        """
        Creates a new SparkSession for the Spark Connect interface.

        Parameters
        ----------
        connectionString: str, optional
            Connection string that is used to extract the connection parameters and configure
            the GRPC connection. Defaults to `sc://localhost`.
        userId : str, optional
            Optional unique user ID that is used to differentiate multiple users and
            isolate their Spark Sessions. If the `user_id` is not set, will default to
            the $USER environment. Defining the user ID as part of the connection string
            takes precedence.
        """
        # Parse the connection string.
        self._client = SparkConnectClient(connectionString)

    def table(self, tableName: str) -> DataFrame:
        return self.read.table(tableName)

    table.__doc__ = PySparkSession.table.__doc__

    @property
    def read(self) -> "DataFrameReader":
        return DataFrameReader(self)

    read.__doc__ = PySparkSession.read.__doc__

    def _inferSchemaFromList(
        self, data: Iterable[Any], names: Optional[List[str]] = None
    ) -> StructType:
        """
        Infer schema from list of Row, dict, or tuple.

        Refer to 'pyspark.sql.session._inferSchemaFromList' with default configurations:

          - 'infer_dict_as_struct' : False
          - 'infer_array_from_first_element' : False
          - 'prefer_timestamp_ntz' : False
        """
        if not data:
            raise ValueError("can not infer schema from empty dataset")
        infer_dict_as_struct = False
        infer_array_from_first_element = False
        prefer_timestamp_ntz = False
        schema = reduce(
            _merge_type,
            (
                _infer_schema(
                    row,
                    names,
                    infer_dict_as_struct=infer_dict_as_struct,
                    infer_array_from_first_element=infer_array_from_first_element,
                    prefer_timestamp_ntz=prefer_timestamp_ntz,
                )
                for row in data
            ),
        )
        if _has_nulltype(schema):
            raise ValueError("Some of types cannot be determined after inferring")
        return schema

    def createDataFrame(
        self,
        data: Union["pd.DataFrame", "np.ndarray", Iterable[Any]],
        schema: Optional[Union[AtomicType, StructType, str, List[str], Tuple[str, ...]]] = None,
    ) -> "DataFrame":
        assert data is not None
        if isinstance(data, DataFrame):
            raise TypeError("data is already a DataFrame")

        _schema: Optional[Union[AtomicType, StructType]] = None
        _schema_str: Optional[str] = None
        _cols: Optional[List[str]] = None

        if isinstance(schema, (AtomicType, StructType)):
            _schema = schema

        elif isinstance(schema, str):
            _schema_str = schema

        elif isinstance(schema, (list, tuple)):
            # Must re-encode any unicode strings to be consistent with StructField names
            _cols = [x.encode("utf-8") if not isinstance(x, str) else x for x in schema]

        if isinstance(data, Sized) and len(data) == 0:
            if _schema is not None:
                return DataFrame.withPlan(LocalRelation(table=None, schema=_schema.json()), self)
            elif _schema_str is not None:
                return DataFrame.withPlan(LocalRelation(table=None, schema=_schema_str), self)
            else:
                raise ValueError("can not infer schema from empty dataset")

        _table: Optional[pa.Table] = None
        _inferred_schema: Optional[StructType] = None

        if isinstance(data, pd.DataFrame):
            from pandas.api.types import (  # type: ignore[attr-defined]
                is_datetime64_dtype,
                is_datetime64tz_dtype,
            )
            from pyspark.sql.pandas.types import (
                _check_series_convert_timestamps_internal,
                _get_local_timezone,
            )

            # First, check if we need to create a copy of the input data to adjust
            # the timestamps.
            input_data = data
            has_timestamp_data = any(
                [is_datetime64_dtype(data[c]) or is_datetime64tz_dtype(data[c]) for c in data]
            )
            if has_timestamp_data:
                input_data = data.copy()
                # We need double conversions for the truncation, first truncate to microseconds.
                for col in input_data:
                    if is_datetime64tz_dtype(input_data[col].dtype):
                        input_data[col] = _check_series_convert_timestamps_internal(
                            input_data[col], _get_local_timezone()
                        ).astype("datetime64[us, UTC]")
                    elif is_datetime64_dtype(input_data[col].dtype):
                        input_data[col] = input_data[col].astype("datetime64[us]")

                # Create a new schema and change the types to the truncated microseconds.
                pd_schema = pa.Schema.from_pandas(input_data)
                new_schema = pa.schema([])
                for x in range(len(pd_schema.types)):
                    f = pd_schema.field(x)
                    # TODO(SPARK-42027) Add support for struct types.
                    if isinstance(f.type, pa.TimestampType) and f.type.unit == "ns":
                        tmp = f.with_type(pa.timestamp("us"))
                        new_schema = new_schema.append(tmp)
                    else:
                        new_schema = new_schema.append(f)
                new_schema = new_schema.with_metadata(pd_schema.metadata)
                _table = pa.Table.from_pandas(input_data, schema=new_schema)
            else:
                _table = pa.Table.from_pandas(data)

        elif isinstance(data, np.ndarray):
            if data.ndim not in [1, 2]:
                raise ValueError("NumPy array input should be of 1 or 2 dimensions.")

            if _cols is None:
                if data.ndim == 1 or data.shape[1] == 1:
                    _cols = ["value"]
                else:
                    _cols = ["_%s" % i for i in range(1, data.shape[1] + 1)]

            if data.ndim == 1:
                if 1 != len(_cols):
                    raise ValueError(
                        f"Length mismatch: Expected axis has 1 element, "
                        f"new values have {len(_cols)} elements"
                    )

                _table = pa.Table.from_arrays([pa.array(data)], _cols)
            else:
                if data.shape[1] != len(_cols):
                    raise ValueError(
                        f"Length mismatch: Expected axis has {data.shape[1]} elements, "
                        f"new values have {len(_cols)} elements"
                    )

                _table = pa.Table.from_arrays(
                    [pa.array(data[::, i]) for i in range(0, data.shape[1])], _cols
                )

        else:
            _data = list(data)

            if _schema is None and isinstance(_data[0], (Row, dict)):
                if isinstance(_data[0], dict):
                    # Sort the data to respect inferred schema.
                    # For dictionaries, we sort the schema in alphabetical order.
                    _data = [dict(sorted(d.items())) for d in _data]

                _inferred_schema = self._inferSchemaFromList(_data, _cols)
                if _cols is not None:
                    for i, name in enumerate(_cols):
                        _inferred_schema.fields[i].name = name
                        _inferred_schema.names[i] = name

            if _cols is None:
                if _schema is None and _inferred_schema is None:
                    if isinstance(_data[0], (list, tuple)):
                        _cols = ["_%s" % i for i in range(1, len(_data[0]) + 1)]
                    else:
                        _cols = ["_1"]
                elif _schema is not None and isinstance(_schema, StructType):
                    _cols = _schema.names
                elif _inferred_schema is not None:
                    _cols = _inferred_schema.names
                else:
                    _cols = ["value"]

            if isinstance(_data[0], Row):
                _table = pa.Table.from_pylist([row.asDict(recursive=True) for row in _data])
            elif isinstance(_data[0], dict):
                _table = pa.Table.from_pylist(_data)
            elif isinstance(_data[0], (list, tuple)):
                _table = pa.Table.from_pylist([dict(zip(_cols, list(item))) for item in _data])
            else:
                # input data can be [1, 2, 3]
                _table = pa.Table.from_pylist([dict(zip(_cols, [item])) for item in _data])

        # Validate number of columns
        num_cols = _table.shape[1]
        if (
            _schema is not None
            and isinstance(_schema, StructType)
            and len(_schema.fields) != num_cols
        ):
            raise ValueError(
                f"Length mismatch: Expected axis has {num_cols} elements, "
                f"new values have {len(_schema.fields)} elements"
            )

        if _cols is not None and len(_cols) != num_cols:
            raise ValueError(
                f"Length mismatch: Expected axis has {num_cols} elements, "
                f"new values have {len(_cols)} elements"
            )

        if _schema is not None:
            return DataFrame.withPlan(LocalRelation(_table, schema=_schema.json()), self)
        elif _schema_str is not None:
            return DataFrame.withPlan(LocalRelation(_table, schema=_schema_str), self)
        elif _inferred_schema is not None:
            return DataFrame.withPlan(LocalRelation(_table, schema=_inferred_schema.json()), self)
        elif _cols is not None and len(_cols) > 0:
            return DataFrame.withPlan(LocalRelation(_table), self).toDF(*_cols)
        else:
            return DataFrame.withPlan(LocalRelation(_table), self)

    createDataFrame.__doc__ = PySparkSession.createDataFrame.__doc__

    def sql(self, sqlQuery: str) -> "DataFrame":
        return DataFrame.withPlan(SQL(sqlQuery), self)

    sql.__doc__ = PySparkSession.sql.__doc__

    def range(
        self,
        start: int,
        end: Optional[int] = None,
        step: int = 1,
        numPartitions: Optional[int] = None,
    ) -> DataFrame:
        if end is None:
            actual_end = start
            start = 0
        else:
            actual_end = end

        if numPartitions is not None:
            numPartitions = int(numPartitions)

        return DataFrame.withPlan(
            Range(
                start=int(start), end=int(actual_end), step=int(step), num_partitions=numPartitions
            ),
            self,
        )

    range.__doc__ = PySparkSession.range.__doc__

    @property
    def catalog(self) -> "Catalog":
        from pyspark.sql.connect.catalog import Catalog

        if not hasattr(self, "_catalog"):
            self._catalog = Catalog(self)
        return self._catalog

    catalog.__doc__ = PySparkSession.catalog.__doc__

    def __del__(self) -> None:
        try:
            # Try its best to close.
            self.client.close()
        except Exception:
            pass

    def stop(self) -> None:
        # Stopping the session will only close the connection to the current session (and
        # the life cycle of the session is maintained by the server),
        # whereas the regular PySpark session immediately terminates the Spark Context
        # itself, meaning that stopping all Spark sessions.
        # It is controversial to follow the existing the regular Spark session's behavior
        # specifically in Spark Connect the Spark Connect server is designed for
        # multi-tenancy - the remote client side cannot just stop the server and stop
        # other remote clients being used from other users.
        self.client.close()

        if "SPARK_LOCAL_REMOTE" in os.environ:
            # When local mode is in use, follow the regular Spark session's
            # behavior by terminating the Spark Connect server,
            # meaning that you can stop local mode, and restart the Spark Connect
            # client with a different remote address.
            active_session = PySparkSession.getActiveSession()
            if active_session is not None:
                active_session.stop()
            with SparkContext._lock:
                del os.environ["SPARK_LOCAL_REMOTE"]
                del os.environ["SPARK_REMOTE"]

    stop.__doc__ = PySparkSession.stop.__doc__

    @classmethod
    def getActiveSession(cls) -> Any:
        raise NotImplementedError("getActiveSession() is not implemented.")

    def newSession(self) -> Any:
        raise NotImplementedError("newSession() is not implemented.")

    @property
    def conf(self) -> Any:
        raise NotImplementedError("conf() is not implemented.")

    @property
    def sparkContext(self) -> Any:
        raise NotImplementedError("sparkContext() is not implemented.")

    @property
    def streams(self) -> Any:
        raise NotImplementedError("streams() is not implemented.")

    @property
    def readStream(self) -> Any:
        raise NotImplementedError("readStream() is not implemented.")

    @property
    def udf(self) -> Any:
        raise NotImplementedError("udf() is not implemented.")

    @property
    def version(self) -> str:
        raise NotImplementedError("version() is not implemented.")

    # SparkConnect-specific API
    @property
    def client(self) -> "SparkConnectClient":
        """
        Gives access to the Spark Connect client. In normal cases this is not necessary to be used
        and only relevant for testing.
        Returns
        -------
        :class:`SparkConnectClient`
        """
        return self._client

    def register_udf(self, function: Any, return_type: Union[str, DataType]) -> str:
        return self._client.register_udf(function, return_type)

    @staticmethod
    def _start_connect_server(master: str) -> None:
        """
        Starts the Spark Connect server given the master.

        At the high level, there are two cases. The first case is development case, e.g.,
        you locally build Apache Spark, and run ``SparkSession.builder.remote("local")``:

        1. This method automatically finds the jars for Spark Connect (because the jars for
          Spark Connect are not bundled in the regular Apache Spark release).

        2. Temporarily remove all states for Spark Connect, for example, ``SPARK_REMOTE``
          environment variable.

        3. Starts a JVM (without Spark Context) first, and adds the Spark Connect server jars
           into the current class loader. Otherwise, Spark Context with ``spark.plugins``
           cannot be initialized because the JVM is already running without the jars in
           the class path before executing this Python process for driver side (in case of
           PySpark application submission).

        4. Starts a regular Spark session that automatically starts a Spark Connect server
           via ``spark.plugins`` feature.

        The second case is when you use Apache Spark release:

        1. Users must specify either the jars or package, e.g., ``--packages
          org.apache.spark:spark-connect_2.12:3.4.0``. The jars or packages would be specified
          in SparkSubmit automatically. This method does not do anything related to this.

        2. Temporarily remove all states for Spark Connect, for example, ``SPARK_REMOTE``
          environment variable. It does not do anything for PySpark application submission as
          well because jars or packages were already specified before executing this Python
          process for driver side.

        3. Starts a regular Spark session that automatically starts a Spark Connect server
          with JVM via ``spark.plugins`` feature.
        """
        session = PySparkSession._instantiatedSession
        if session is None or session._sc._jsc is None:
            conf = SparkConf()
            # Do not need to worry about the existing configurations because
            # Py4J gateway is not created yet, and `conf` instance is empty here.
            # The configurations belows are manually manipulated later to respect
            # the user-specified configuration first right after Py4J gateway creation.
            conf.set("spark.master", master)
            conf.set("spark.plugins", "org.apache.spark.sql.connect.SparkConnectPlugin")
            conf.set("spark.local.connect", "1")

            # Check if we're using unreleased version that is in development.
            # Also checks SPARK_TESTING for RC versions.
            is_dev_mode = (
                "dev" in LooseVersion(__version__).version or "SPARK_TESTING" in os.environ
            )
            origin_remote = os.environ.get("SPARK_REMOTE", None)
            try:
                if origin_remote is not None:
                    # So SparkSubmit thinks no remote is set in order to
                    # start the regular PySpark session.
                    del os.environ["SPARK_REMOTE"]

                connect_jar = None
                if is_dev_mode:
                    # Try and catch for a possibility in production because pyspark.testing
                    # does not exist in the canonical release.
                    try:
                        from pyspark.testing.utils import search_jar

                        # Note that, in production, spark.jars.packages configuration should be
                        # set by users. Here we're automatically searching the jars locally built.
                        connect_jar = search_jar(
                            "connector/connect/server", "spark-connect-assembly-", "spark-connect"
                        )
                        if connect_jar is None:
                            warnings.warn(
                                "Attempted to automatically find the Spark Connect jars because "
                                "'SPARK_TESTING' environment variable is set, or the current "
                                f"PySpark version is dev version ({__version__}). However, the jar"
                                " was not found. Manually locate the jars and specify them, e.g., "
                                "'spark.jars' configuration."
                            )
                    except ImportError:
                        pass

                # Note that JVM is already up at this point in the case of Python
                # application submission.
                with SparkContext._lock:
                    if not SparkContext._gateway:
                        SparkContext._gateway = launch_gateway(conf)
                        SparkContext._jvm = SparkContext._gateway.jvm
                        if connect_jar is not None:
                            SparkContext._jvm.PythonSQLUtils.addJarToCurrentClassLoader(connect_jar)

                        # Now, JVM is up, and respect the default set.
                        prev = conf
                        conf = SparkConf(_jvm=SparkContext._jvm)
                        conf.set("spark.master", master)
                        for k, v in prev.getAll():
                            if not conf.contains(k):
                                conf.set(k, v)

                # The regular PySpark session is registered as an active session
                # so would not be garbage-collected.
                PySparkSession(SparkContext.getOrCreate(conf))
            finally:
                if origin_remote is not None:
                    os.environ["SPARK_REMOTE"] = origin_remote
        else:
            raise RuntimeError("There should not be an existing Spark Session or Spark Context.")


SparkSession.__doc__ = PySparkSession.__doc__


def _test() -> None:
    import os
    import sys
    import doctest
    from pyspark.sql import SparkSession as PySparkSession
    from pyspark.testing.connectutils import should_test_connect, connect_requirement_message

    os.chdir(os.environ["SPARK_HOME"])

    if should_test_connect:
        import pyspark.sql.connect.session

        globs = pyspark.sql.connect.session.__dict__.copy()
        globs["spark"] = (
            PySparkSession.builder.appName("sql.connect.session tests")
            .remote("local[4]")
            .getOrCreate()
        )

        # Uses PySpark session to test builder.
        globs["SparkSession"] = PySparkSession
        # Spark Connect does not support to set master together.
        pyspark.sql.connect.session.SparkSession.__doc__ = None
        del pyspark.sql.connect.session.SparkSession.Builder.master.__doc__
        # RDD API is not supported in Spark Connect.
        del pyspark.sql.connect.session.SparkSession.createDataFrame.__doc__

        # TODO(SPARK-41811): Implement SparkSession.sql's string formatter
        del pyspark.sql.connect.session.SparkSession.sql.__doc__

        (failure_count, test_count) = doctest.testmod(
            pyspark.sql.connect.session,
            globs=globs,
            optionflags=doctest.ELLIPSIS
            | doctest.NORMALIZE_WHITESPACE
            | doctest.IGNORE_EXCEPTION_DETAIL,
        )

        globs["spark"].stop()

        if failure_count:
            sys.exit(-1)
    else:
        print(
            f"Skipping pyspark.sql.connect.session doctests: {connect_requirement_message}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    _test()
