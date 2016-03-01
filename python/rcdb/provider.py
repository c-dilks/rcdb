"""@package AlchemyProvider
Documentation for this module.

More details.
"""
import os

import re
import logging
import sys
from collections import MutableSequence
from time import mktime

from rcdb.alias import default_aliases

from ply.lex import LexToken

from log_format import BraceMessage as Lf
from rcdb import lexer
from rcdb.stopwatch import StopWatchTimer
from .errors import OverrideConditionTypeError, NoConditionTypeFound, NoRunFoundError, OverrideConditionValueError, \
    QueryFormatError
import sqlalchemy.orm
from sqlalchemy.orm import Session, contains_eager
from sqlalchemy.orm import joinedload, aliased
import datetime

import sqlalchemy
from model import *

import posixpath
import file_archiver
from rcdb.constants import COMPONENT_STAT_KEY
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.orm.strategy_options import subqueryload, joinedload

log = logging.getLogger("rcdb.provider")


class RCDBProvider(object):
    """ RCDB data provider that uses SQLAlchemy for accessing databases """

    def __init__(self, connection_string=None, user_name=""):
        self._is_connected = False
        self.path_name_regex = re.compile('^[\w\-_]+$', re.IGNORECASE)
        self._connection_string = ""
        self.logging_enabled = True
        self.engine = None
        self.session = None

        # username for record
        self.user_name = user_name
        """:type: str"""
        if not user_name:
            if "RCDB_USER" in os.environ.keys():
                self.user_name = os.environ["RCDB_USER"]

        if connection_string:
            self.connect(connection_string)
        """:type : Session """

    # ------------------------------------------------
    # Connects to database using connection string
    # ------------------------------------------------
    def connect(self, connection_string="mysql+mysqlconnector://rcdb@127.0.0.1/rcdb"):
        """
        Connects to database using connection string

        connection string might be in form:
        mysql://<username>:<password>@<mysql.address>:<port> <database>
        sqlite:///path/to/file.sqlite

        :param connection_string: connection string
        :type connection_string: str
        """

        try:
            self.engine = sqlalchemy.create_engine(connection_string)
        except ImportError, err:
            # sql alchemy uses MySQLdb by default. But it might be not install in the system
            # in such case we fallback to mysqlconnector which is embedded in CCDB
            if connection_string.startswith("mysql://") and "No module named MySQLdb" in repr(err):
                connection_string = connection_string.replace("mysql://", "mysql+mysqlconnector://")
                self.engine = sqlalchemy.create_engine(connection_string)
            else:
                raise

        session_type = sessionmaker(bind=self.engine)
        self.session = session_type()
        self._is_connected = True
        self._connection_string = connection_string

    # ------------------------------------------------
    # Closes connection to data
    # ------------------------------------------------
    def disconnect(self):
        """Closes connection to database"""
        self._is_connected = False
        self.session.close()

    # -------------------------------------------------
    # indicates ether the connection is open or not
    # -------------------------------------------------
    @property
    def is_connected(self):
        """
        indicates ether the connection is open or not

        :return: bool True if connection is opened
        :rtype: bool
        """
        return self._is_connected

    # ------------------------------------------------
    # Connection string that was used
    # ------------------------------------------------
    @property
    def connection_string(self):
        """
        Connection string that was used on last connect()

        :return: connection string
        :rtype: str
        """
        return self._connection_string

    # -------------------------------------------------------------------
    # Adds log record to the database
    # -------------------------------------------------------------------
    def add_log_record(self, table_ids, description, related_run_number):
        """
        Adds log record to the database
        :param table_ids: Str in form tablename_id, or list of such strings, or ModelBase object, or list[ModelBase]
        :type table_ids:list[str] or list[ModelBase] or Base or str

        :param description: Text description of what has been done
        :type description: str

        :param related_run_number: If it is possible a run number to which this record corresponds
        :type related_run_number: int

        :return:
        """

        if isinstance(related_run_number, Run):
            related_run_number = related_run_number.number

        record = LogRecord()

        # table ids?
        if isinstance(table_ids, Base):
            record.table_ids = table_ids.log_id
        elif isinstance(table_ids, list):
            if table_ids:
                if isinstance(table_ids[0], ModelBase):
                    record.table_ids = list_to_db_text([item.log_id for item in table_ids])
                elif isinstance(table_ids[0], str):
                    record.table_ids = list_to_db_text(table_ids)
        elif isinstance(table_ids, str):
            record.table_ids = table_ids

        # description
        record.description = str(description)
        record.related_run_number = related_run_number

        if self.user_name:
            record.user_name = self.user_name

        # save
        self.session.add(record)
        self.session.commit()
        log.info(description)

    # ------------------------------------------------
    # Gets Run or returns None
    # ------------------------------------------------
    def get_run(self, run_number):
        """Gets Run object from run_number
            :param run_number: the run number
            :param run_number: int

            :return: Run object corresponding to run number or None if there is no such run in DB
            :rtype: Run or None
        """
        if isinstance(run_number, Run):
            return run_number

        run_number = int(run_number)

        query = self.session.query(Run).filter(Run.number == run_number)
        return query.first()

    # ------------------------------------------------
    # Gets Runs in range [rum_min, run_max]
    # ------------------------------------------------
    def get_runs(self, rum_min, run_max):

        """ Gets all runs that rum_min<= run.number <= run_max

        :type run_max: int
        :type rum_min: int
        """
        return self.session.query(Run).filter(Run.number >= rum_min, Run.number <= run_max) \
            .order_by(Run.number).all()

    # ------------------------------------------------
    # Gets Run or returns None
    # ------------------------------------------------
    def get_next_run(self, run_or_number):
        """Gets run which number is the next to given run_number
            :param run_or_number: the run number
            :param run_or_number: int, Run

            :return: Run object corresponding to run number or None if there is no such run in DB
            :rtype: Run or None
        """
        if isinstance(run_or_number, Run):
            run_number = run_or_number.number
        else:
            run_number = run_or_number

        query = self.session.query(Run).filter(Run.number > run_number).order_by(Run.number)
        return query.first()

    # ------------------------------------------------
    # Gets Run or returns None
    # ------------------------------------------------
    def get_prev_run(self, run_or_number):
        """Gets run which number is the previous to given run_number
            :param run_or_number: the run number
            :param run_or_number: int, Run

            :return: Run object corresponding to run number or None if there is no such run in DB
            :rtype: Run or None
        """
        if isinstance(run_or_number, Run):
            run_number = run_or_number.number
        else:
            run_number = run_or_number

        query = self.session.query(Run).filter(Run.number < run_number).order_by(Run.number.desc())
        return query.first()

    # ------------------------------------------------
    # Gets or creates Run
    # ------------------------------------------------
    def create_run(self, run_number):
        """Gets or creates Run with given number
            :type run_number: int
            :rtype: Run
        """
        run = self.get_run(run_number)
        if not run:
            # no run is found

            run = Run()
            run.number = run_number
            self.session.add(run)
            self.session.commit()

        return run

    # ------------------------------------------------
    # Returns condition type
    # ------------------------------------------------
    def get_condition_type(self, name):
        """Gets condition type by name

        :param name: name of condition
        :type name: str

        :return: ConditionType corresponding to name
        :rtype: ConditionType
        """
        try:
            return self.session.query(ConditionType).filter(ConditionType.name == name).one()
        except NoResultFound:
            message = "No ConditionType with name='{}' is found in DB".format(name)
            raise NoConditionTypeFound(message)

    # ------------------------------------------------
    # Returns condition type
    # ------------------------------------------------
    def get_condition_types(self):
        """Gets all condition types as list of ConditionType

        :return: all ConditionTypes in db
        :rtype: dict, {ConditionType}
        """
        try:
            return self.session.query(ConditionType).all()
        except NoResultFound:
            return []

    # ------------------------------------------------
    # Creates condition type
    # ------------------------------------------------
    def create_condition_type(self, name, value_type, description):
        """
        Creates condition type

        :param name: name that is used to retrieve conditions
        :type name: str

        :param value_type: It is one of ConditionType.FI
        :type value_type: str

        :param description: Short description of the condition. 255 chars max
        :type description: basestring

        :return: ConditionType object that corresponds to created DB record
        :rtype: ConditionType
        """

        query = self.session.query(ConditionType).filter(ConditionType.name == name)

        if query.count():
            # we've found such type!
            ct = query.first()
            assert isinstance(ct, ConditionType)

            if ct.value_type != value_type:
                message = "Condition type with this name exists, but value type is different:" \
                          "Database value_type={}, new value_type={}".format(ct.value_type, value_type)

                raise OverrideConditionTypeError(message)

            if ct.description != description:
                message = "Condition type with this name exists, but description is different:" \
                          "Database description={}, new description={}".format(ct.description, description)

                raise OverrideConditionTypeError(message)

            # if we are here, selected ct is the same as requested
            return ct
        else:
            # no such ConditionType found in the database
            ct = ConditionType()
            ct.name = name
            ct.value_type = value_type
            ct.description = description
            self.session.add(ct)
            self.session.commit()
            self.add_log_record(ct, "ConditionType created with name='{}', type='{}'"
                                .format(name, value_type), 0)
            return ct

    # ------------------------------------------------
    # Adds condition value to database
    # ------------------------------------------------
    def add_condition(self, run, key, value, replace=False, auto_commit=True):
        """ Adds condition value for the run

        Only one value is allowed for a run. If run already has this condition:
            1. It value is the same the func does nothing
            2. If value is different than in DB, function check 'replace' flag and do accordingly

        Example:
            db.add_condition_value(1, "event_count", 1000)                  # Ok. First addition to DB
            db.add_condition_value(1, "event_count", 1000)                  # Ok. Do nothing, such value already exists
            db.add_condition_value(1, "event_count", 2222)                  # Error. OverrideConditionValueError
            db.add_condition_value(1, "event_count", 2222, replace=True)    # Ok. Replacing existing value
            print(db.get_condition(1, "event_count"))
                value: 2222
                time:  None

        Performance:
            Setting auto_commit=False allows to improve performance when adding many conditions for the run
            Example:
                for name, value in values_to_add:
                    db.add_condition_value(run, name, value, auto_commit=False)

                # Commit all added values to DB
                db.session.commit()

        :param run: The run number for this condition value
        :type run: int, Run

        :param value: The value of the condition.

        :param key: name of condition or ConditionType
        :type key: str, ConditionType

        :param replace: If true, function replaces existing value
        :type replace: bool

        :param auto_commit: If true, changes to DB are committed as values are changed.
                            If false, user should call this.session.commit() to commit the changes

        :return: Condition object from DB
        :rtype: Condition
        """

        # get run for the condition
        if not isinstance(run, Run):  # run is given as run number not Run object
            run_number = run
            run = self.get_run(run_number)
        else:
            run_number = run.number

        if not run:
            message = "No run with run_number='{}' found".format(run_number)
            raise NoRunFoundError(message)

        # get type
        if isinstance(key, ConditionType):
            ct = key
        else:
            assert isinstance(key, str)
            ct = self.get_condition_type(key)

        # validate value
        if ct.value_type == ConditionType.FLOAT_FIELD:
            try:
                value = float(value)
            except ValueError as err:
                message = "Condition type '{}' awaits float as value. {}".format(ct, err)
                raise ValueError(message)
        elif ct.value_type == ConditionType.INT_FIELD:
            try:
                value = int(value)
            except ValueError as err:
                message = "Condition type '{}' awaits int as value. {}".format(ct, err)
                raise ValueError(message)
        elif ct.value_type == ConditionType.TIME_FIELD and not isinstance(value, datetime.datetime):
            message = "Condition type '{}' awaits datetime as value. '{}' is given".format(ct, type(value))
            raise ValueError(message)
        elif ct.value_type == ConditionType.BOOL_FIELD:
            try:
                value = bool(value)
            except ValueError as err:
                message = "Condition type '{}' awaits bool as value. {}".format(ct, err)
                raise ValueError(message)

        # Check! maybe ve have such condition value for this run
        db_condition = self.get_condition(run, ct)

        if db_condition is not None:
            # one per run situation
            assert isinstance(db_condition, Condition)

            # if value is float, use precision
            if ct.value_type == ConditionType.FLOAT_FIELD:
                # value is float here because of the validation above
                value_is_differ = abs(db_condition.value - value) <= 1e-14
            else:
                value_is_differ = db_condition.value != value

            if value_is_differ:
                if replace:
                    # We have to replace the old value
                    db_condition.value = value
                    db_condition.created = datetime.datetime.now()
                    if auto_commit:
                        self.session.commit()
                        return db_condition
                else:
                    message = "Condition {} already exists for the run_number='{}' " \
                              "but the value is different. DB saved value='{}', new value='{}'. " \
                              "(Add replace=True if you want to replace the old value)" \
                        .format(ct.name, run, db_condition.value, value)
                    raise OverrideConditionValueError(message)

            # values are the same
            return db_condition

        # we haven't found the field in db
        condition = Condition()
        condition.type = ct
        condition.run = run
        condition.value = value
        self.session.add(condition)

        if auto_commit:
            self.session.commit()

        return condition

    # ------------------------------------------------
    # Gets condition
    # ------------------------------------------------
    def get_condition(self, run_number, key):
        """ Returns condition value for the run
        If the is_many_per_run flag is allowed per condition, this function returns one of it
        get_condition_list returns multiple values

        :param run_number: the run number
        :type run_number: Run or int

        :param key: Condition name or ConditionType object
        :type key: str or ConditionType

        :return: Value or None if no such ConditionValue attached to the run
        :rtype: Condition
        """
        if isinstance(run_number, Run):
            run_number = run_number.number
        else:
            run_number = int(run_number)

        if isinstance(key, ConditionType):
            ct = key
        else:
            assert isinstance(key, str)
            ct = self.get_condition_type(key)

        query = self.session.query(Condition). \
            filter(Condition.type == ct, Condition.run_number == run_number)

        return query.first()

    def select_runs(self, search_str, run_min=0, run_max=sys.maxsize, sort_desc=False):
        """ Searches RCDB for runs with e

        :param sort_desc: if True result runs will by sorted descendant by run_number, ascendant if False
        :param run_min: minimum run to search
        :param run_max: maximum run to search
        :param search_str: Search pattern
        :type search_str: str
        :return: List of runs matching criteria
        :rtype: list(Run)
        """
        start_time_stamp = int(mktime(datetime.datetime.now().timetuple()) * 1000)
        preparation_sw = StopWatchTimer()
        preparation_sw.start()

        if run_min > run_max:
            run_min, run_max = run_max, run_min

        # get all condition types
        all_cnt_types = self.get_condition_types()
        all_cnd_types_by_name = {cnd.name: cnd for cnd in all_cnt_types}
        all_cnd_names = [str(key) for key in all_cnd_types_by_name.keys()]

        # PHASE 1: getting what to search from search_str

        search_str = str(search_str)
        if '__' in search_str:
            raise QueryFormatError("Query contains restricted symbol: '__'")

        for alias in default_aliases:
            al_name = "@" + alias.name
            if al_name in search_str:
                search_str = search_str.replace(al_name, '(' + alias.expression + ')')

        search_str = search_str.replace('\n', ' ')
        search_str = search_str.replace('\r', ' ')

        tokens = [token for token in lexer.tokenize(search_str)]

        target_cnd_types = []
        names = []
        aliased_cnd_types = []
        aliased_cnd = []
        names_count = 0
        for token in tokens:
            if token.type in lexer.rcdb_query_restricted:
                raise QueryFormatError("Query contains restricted symbol: '{}'".format(token.value))

            if token.type != "NAME":
                continue

            if token.value == 'math':
                continue

            if token.value == 'startswith':
                continue

            if token.value not in all_cnd_names:
                message = "Name '{}' is not found in ConditionTypes".format(token.value)
                raise QueryFormatError(message)
            else:
                cnd_name = token.value
                cnd_type = all_cnd_types_by_name[token.value]
                target_cnd_types.append(cnd_type)

                token.value = "value[{}].value".format(names_count)
                names_count += 1

                names.append(cnd_name)
                aliased_cnd.append(aliased(Condition))
                aliased_cnd_types.append(aliased(ConditionType))

        # PHASE 2: Database query
        query = self.session.query()

        if not names_count:
            return None

        search_eval = " ".join([token.value for token in tokens if isinstance(token, LexToken)])

        for (i, alias_cnd) in enumerate(aliased_cnd):
            query = query.add_entity(alias_cnd).filter(alias_cnd._condition_type_id == target_cnd_types[i].id)
            if i != 0:
                query = query.filter(alias_cnd.run_number == aliased_cnd[0].run_number)

        query = query.filter(aliased_cnd[0].run_number >= run_min, aliased_cnd[0].run_number <= run_max) \
            .join(aliased_cnd[0].run)

        # apply sorting
        if not sort_desc:
            query = query.order_by(aliased_cnd[0].run_number)
        else:
            query = query.order_by(desc(aliased_cnd[0].run_number))

        preparation_sw.stop()
        query_sw = StopWatchTimer()
        query_sw.start()

        values = query.all()

        query_sw.stop()

        selection_sw = StopWatchTimer()
        selection_sw.start()

        # PHASE 3: Selecting runs
        compiled_search_eval = compile(search_eval, '<string>', 'eval')

        sel_runs = []

        for value in values:
            if isinstance(value, Condition):
                value = (value,)
            run = value[0].run
            if eval(compiled_search_eval):
                sel_runs.append(run)

        selection_sw.stop()
        result = RunSelectionResult(sel_runs, self)
        result.filter_condition_names = names
        result.filter_condition_types = target_cnd_types
        result.performance["preparation"] = preparation_sw.elapsed
        result.performance["query"] = query_sw.elapsed
        result.performance["selection"] = selection_sw.elapsed
        result.performance["start_time_stamp"] = start_time_stamp

        return result


class RunSelectionResult(MutableSequence):
    """Define a list format, which I can customize"""

    def __init__(self, runs=None, db=None):
        super(RunSelectionResult, self).__init__()
        self.filter_condition_types = []
        self.filter_condition_names = []
        self.selected_conditions = []
        self.db = db

        js_now = int(mktime(datetime.datetime.now().timetuple())* 1000)
        self.performance = {"preparation": 0,
                            "query": 0,
                            "selection": 0,
                            "start_time_stamp": js_now,
                            "get_conditions": 0,
                            "tabling_values": 0}

        if runs is not None:
            self.runs = list(runs)
        else:
            self.runs = list()

    def __len__(self):
        return len(self.runs)

    def __getitem__(self, ii):
        return self.runs[ii]

    def __delitem__(self, ii):
        del self.runs[ii]

    def __setitem__(self, ii, val):
        self.runs[ii] = val
        return self.runs[ii]

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        return """<RunSelectionResult filer_cnd_names:{}, Runs:{}>""".format(self.filter_condition_names, self.runs)

    def insert(self, ii, val):
        self.runs.insert(ii, val)

    def append(self, val):
        list_idx = len(self.runs)
        self.insert(list_idx, val)

    def get_values(self, condition_names, insert_run_number=False):

        if self.db is None or not self.runs:
            return [[]]

        target_cnd_names = condition_names

        sw = StopWatchTimer()
        sw.start()

        all_cnt_types = self.db.get_condition_types()
        all_cnd_types_by_name = {cnd.name: cnd for cnd in all_cnt_types}

        # getting target conditions types and sorting them by id
        target_cnd_types = [all_cnd_types_by_name[cnd_name] for cnd_name in target_cnd_names]
        target_cnd_types = sorted(target_cnd_types, key=lambda x: x.id)
        target_cnd_types_len = len(target_cnd_types)

        ids = [ct.id for ct in target_cnd_types]
        run_numbers = [r.number for r in self.runs]

        query = self.db.session.query(Condition)\
            .filter(Condition._condition_type_id.in_(ids), Condition.run_number.in_(run_numbers))\
            .order_by(Condition.run_number, Condition._condition_type_id)

        conditions = query.all()

        # performance measure
        sw.stop()
        self.performance["get_conditions"] = sw.elapsed
        sw = StopWatchTimer()
        sw.start()

        rows = []

        def get_empty_row(run_number=0):
            if insert_run_number:
                return [run_number] + ([None] * target_cnd_types_len)
            else:
                return [None] * target_cnd_types_len

        type_index = 0
        prev_run = conditions[0].run_number
        conditions_iter = 0
        conditions_len = len(conditions)

        row = get_empty_row(self.runs[0].number)
        run_index=0

        while self.runs[run_index].number != prev_run:
            rows.append(row)
            run_index += 1
            row = get_empty_row(self.runs[run_index].number)

        for condition in conditions:
            assert isinstance(condition, Condition)
            conditions_iter += 1

            type_id = condition._condition_type_id
            if condition.run_number != prev_run or conditions_len == conditions_iter:
                rows.append(row)
                prev_run = condition.run_number

                while self.runs[run_index].number != prev_run:
                    rows.append(row)
                    run_index += 1
                    row = get_empty_row(self.runs[run_index].number)

                if conditions_len != conditions_iter:
                    type_index = 0

            while type_index < target_cnd_types_len and type_id != target_cnd_types[type_index].id:
                type_index += 1
                if type_index == target_cnd_types_len:
                    type_index = 0

            if insert_run_number:
                row[type_index + 1] = condition
            else:
                row[type_index] = condition

        # performance measure
        sw.stop()
        self.performance["tabling_values"] = sw.elapsed
        return rows



class ConfigurationProvider(RCDBProvider):
    """
    RCDB data provider that uses SQLAlchemy for accessing databases
    """

    # ------------------------------------------------
    #
    # ------------------------------------------------
    def obtain_board(self, board_type, serial):
        query = self.session.query(Board).filter(Board.board_type == board_type, Board.serial == serial)
        if not query.count():
            log.debug(Lf("Board type='{}' sn='{}' is not found in DB. Creating record", board_type, serial))
            board = Board()
            board.serial = serial
            board.board_type = board_type
            self.session.add(board)
            self.session.commit()
            log.info(Lf("Board type='{}' sn='{}' added to DB", board_type, serial))
            return board
        else:
            return query.first()

    # ---------------------------
    #
    # ---------------------------
    def obtain_crate(self, name):
        """
        Gets or creates crate with the name
        """
        query = self.session.query(Crate).filter(Crate.name == name)
        if not query.count():
            log.debug(Lf("Crate '{}' is not found in DB. Creating record...", name))
            crate = Crate()
            crate.name = name
            self.session.add(crate)
            self.session.commit()
            log.info(Lf("Crate '{}' is added to DB", name))
            return crate
        else:
            return query.first()

    # -----------------------------------------------------
    #
    # ------------------------------------------------------
    def obtain_board_installation(self, crate, board, slot):
        """
        Gets board installation by crate, board, slot. Create a new one in DB
        if there is no such installation
        """
        # some validation and value checks
        if isinstance(crate, basestring):
            crate = self.obtain_crate(crate)

        if isinstance(board, tuple):
            board_type, serial = board
            board = self.obtain_board(board_type, serial)
        assert isinstance(crate, Crate)
        assert isinstance(board, Board)
        slot = int(slot)

        query = self.session.query(BoardInstallation).filter(BoardInstallation.board_id == board.id,
                                                             BoardInstallation.crate_id == crate.id,
                                                             BoardInstallation.slot == slot)
        if not query.count():
            log.debug(Lf("Board installation for crate='{}', "
                         "board='{}', sn='{}', slot='{}' is not found in DB. Creating record...",
                         crate.name, board.board_type, board.serial, slot))
            installation = BoardInstallation()
            installation.board = board
            installation.crate = crate
            installation.slot = slot
            self.session.add(installation)
            self.session.commit()
            self.add_log_record(installation,
                                "Board installation for crate='{}', board='{}', sn='{}', slot='{}' added to DB".format(
                                    crate.name, board.board_type, board.serial, slot),
                                0)
            return installation
        else:
            return query.first()

    # ------------------------------------------------
    #
    # ------------------------------------------------
    def obtain_dac_preset(self, board, values):
        """Gets or creates dac preset for board and dac values"""
        query = self.session.query(DacPreset) \
            .filter(DacPreset.board_id == board.id,
                    DacPreset.text_values == list_to_db_text(values))
        if not query.count():
            preset = DacPreset()
            preset.board = board
            preset.values = values
            self.session.add(preset)
            self.session.commit()
        else:
            preset = query.first()

        assert isinstance(preset, DacPreset)
        return preset

    # ------------------------------------------------
    #
    # ------------------------------------------------
    def add_board_config_to_run(self, run, board, dac_preset):
        """sets that the board have the dac preset values in the run"""
        if not isinstance(run, Run):  # run is given as run number not Run object
            run = self.create_run(int(run))

        if not isinstance(dac_preset, DacPreset):
            dac_preset = self.obtain_dac_preset(board, dac_preset)

        # query = self.session.query(BoardConfiguration).join(BoardConfiguration.runs) \
        # .filter(RunConfiguration.id == run.id,
        # BoardConfiguration.board_id == board.id,
        # BoardConfiguration.dac_preset_id == dac_preset.id)

        query = self.session.query(BoardConfiguration) \
            .filter(BoardConfiguration.board_id == board.id,
                    BoardConfiguration.dac_preset_id == dac_preset.id)

        # Get or create board configuration
        if not query.count():
            log.debug(Lf("Board configuration for board.id='{}', dac_preset.id='{}' not found",
                         board.id, dac_preset.id))
            board_config = BoardConfiguration()
            board_config.board = board
            board_config.dac_preset = dac_preset
            self.session.add(board_config)
            self.session.commit()
            self.add_log_record([board_config, board, dac_preset],
                                "Board conf create. board.id='{}', dac_preset.id='{}'".format(board.id, dac_preset.id),
                                run.number)
        else:
            board_config = query.first()

        # check for run!
        if run not in board_config.runs:
            log.debug(Lf("Board configuration id='{}' not found in run='{}'", board_config.id, run.number))
            board_config.runs.append(run)
            self.session.commit()
            self.add_log_record(board_config,
                                "Board conf id='{}' added to run='{}'".format(board_config.id, run.number),
                                run.number)
        else:
            log.debug(Lf("Board configuration id='{}' is already in run='{}'", board_config.id, run.number))

    # ------------------------------------------------
    #
    # ------------------------------------------------
    def add_board_installation_to_run(self, run, board_installation):
        """Adds board installation to run using crate, board, slot
        :run: run number or RunConfiguration object
        :board_installation: board installation object
        """
        if isinstance(board_installation, tuple):
            # it is (crate, board, slot)
            crate, board, slot = board_installation
            board_installation = self.obtain_board_installation(crate, board, slot)

        if not isinstance(run, Run):  # run is given as run number not Run object
            run = self.create_run(int(run))
        assert isinstance(board_installation, BoardInstallation)

        if board_installation not in run.board_installations:
            log.debug(Lf("Board installation id='{}' is not associated with run='{}'",
                         board_installation.id, run.number))
            run.board_installations.append(board_installation)
            self.session.commit()
            self.add_log_record(board_installation,
                                "Add board_installation='{}' to run='{}'".format(board_installation.id, run.number),
                                run.number)
        else:
            log.debug(Lf("Board installation id='{}' already associated with run='{}'",
                         board_installation.id, run.number))

    # ------------------------------------------------
    #
    # ------------------------------------------------
    def add_run_statistics(self, run, total_events):
        """adds run statistics like total events number, etc"""
        if not isinstance(run, Run):  # run is given as run number not Run object
            run = self.create_run(int(run))

        run.total_events = total_events
        log.debug(Lf("Updating run statistics. total_events='{}'", total_events))

        self.session.commit()
        self.add_log_record(run, "Run statistics updated. total_events='{}'. Etc...".format(total_events), run.number)

    # ------------------------------------------------
    #
    # ------------------------------------------------
    def add_run_start_time(self, run, dtm):
        """
            Sets staring time of run
        """
        assert (isinstance(dtm, datetime.datetime))

        if not isinstance(run, Run):  # run is given as run number not Run object
            run = self.create_run(int(run))

        if run.start_time == dtm:
            return

        log.debug(Lf("Setting start time '{}' to run '{}'", dtm, run.number))

        run.start_time = dtm
        self.session.commit()

    # ------------------------------------------------
    #
    # ------------------------------------------------
    def add_run_end_time(self, run, dtm):
        """Adds time of run"""
        assert (isinstance(dtm, datetime.datetime))

        if not isinstance(run, Run):  # run is given as run number not Run object
            run = self.create_run(int(run))

        if run.end_time == dtm:
            return

        log.debug(Lf("Setting end time '{}' to run '{}'", dtm, run.number))
        run.end_time = dtm
        self.session.commit()

    # ------------------------------------------------
    #
    # ------------------------------------------------
    def add_configuration_file(self, run, path, content=None, overwrite=False):
        """Adds configuration file to run configuration. If such file exists
        :param overwrite: If this flag is true, such file for this run exists but checksumm is different,
                          file content will be overwritten
        :param content: Content of a file. If not given, func tryes to open file by path.
        :param path: Path of the file
        :param run: Run number
        """

        def get_content():
            if content:
                return content
            with open(path) as io_file:
                return io_file.read()

        log.debug("Processing configuration file")

        if content is None:
            log.debug(Lf("|- Content is None, assuming using file '{}'", path))
            check_sum = file_archiver.get_file_sha256(path)
        else:
            log.debug(Lf("|- Content is NOT none, using it to put to DB", path))
            check_sum = file_archiver.get_string_sha256(content)

        if not isinstance(run, Run):  # run is given as run number not Run object
            run = self.create_run(run)

        if overwrite:
            # If we have to potentially overwrite the file, we have to apply another logic
            # First, we look at file with this name in this run
            query = self.session.query(ConfigurationFile) \
                .filter(ConfigurationFile.runs.contains(run)) \
                .filter(ConfigurationFile.path == path) \
                .order_by(desc(ConfigurationFile.id))  # we want latest
            if query.count():
                # There are file to overwrite!
                conf_file = query.first()
                conf_file.sha256 = check_sum
                conf_file.path = path
                conf_file.content = get_content()
                log.debug(Lf("|- File '{}' is getting overwritten", path))

                self.session.commit()
                return conf_file

        # Overwrite = false or is not possible
        # Look, do we have a file with such name and checksumm?
        file_query = self.session.query(ConfigurationFile) \
            .filter(ConfigurationFile.sha256 == check_sum, ConfigurationFile.path == path)

        if not file_query.count():
            # no such file found!
            log.debug(Lf("|- File '{}' not found in DB", path))

            # create file.
            conf_file = ConfigurationFile()
            conf_file.sha256 = check_sum
            conf_file.path = path
            conf_file.content = get_content()

            # put it to DB and associate with run
            self.session.add(conf_file)
            self.session.commit()

            conf_file.runs.append(run)

            # save and exit
            self.session.commit()
            self.add_log_record(conf_file, "File added to DB. Path: '{}'. Run: '{}'".format(path, run), run.number)
            return conf_file

        # such file already exists! Get it from database
        conf_file = file_query.first()
        log.debug(Lf("|- File '{}' found in DB by id: '{}'", path, conf_file.id))

        # maybe... we even have this file in run conf?
        if conf_file not in run.files:
            conf_file.runs.append(run)
            # run_conf.files.append(conf_file)
            self.session.commit()  # save and exit
            self.add_log_record(conf_file, "File associated. Path: '{}'. Run: '{}'".format(path, run), run.number)
        else:
            log.debug(Lf("|- File already associated with run'{}'", run))

        return conf_file
