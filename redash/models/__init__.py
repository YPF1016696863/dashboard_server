import cStringIO
import calendar
import csv
import datetime
import logging
import time

import pytz
import xlsxwriter
from six import python_2_unicode_compatible, text_type
from sqlalchemy import distinct, or_, and_, UniqueConstraint
from sqlalchemy import func
from sqlalchemy.dialects import postgresql
from sqlalchemy.event import listens_for
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import backref, contains_eager, joinedload, subqueryload, load_only
from sqlalchemy_utils import generic_relationship
from sqlalchemy_utils.models import generic_repr
from sqlalchemy_utils.types import TSVectorType
from sqlalchemy_utils.types.encrypted.encrypted_type import FernetEngine

from redash import redis_connection, utils, settings
from redash.destinations import (get_configuration_schema_for_destination_type,
                                 get_destination)
from redash.models.parameterized_query import ParameterizedQuery
from redash.query_runner import (get_configuration_schema_for_query_runner_type,
                                 get_query_runner)
from redash.utils import generate_token, json_dumps, json_loads
from redash.utils.configuration import ConfigurationContainer
from .base import db, gfk_type, Column, GFKBase, SearchBaseQuery
from .changes import ChangeTrackingMixin, Change  # noqa
from .mixins import BelongsToOrgMixin, TimestampMixin
from .organizations import Organization
from .types import EncryptedConfiguration, Configuration, MutableDict, MutableList, PseudoJSON
from .users import (AccessPermission, AnonymousUser, ApiUser, Group, User)  # noqa
from redash.permissions import (can_modify, require_admin_or_owner,
                                require_object_modify_permission,has_permission,
                                require_permission)

logger = logging.getLogger(__name__)


class ScheduledQueriesExecutions(object):
    KEY_NAME = 'sq:executed_at'

    def __init__(self):
        self.executions = {}

    def refresh(self):
        self.executions = redis_connection.hgetall(self.KEY_NAME)

    def update(self, query_id):
        redis_connection.hmset(self.KEY_NAME, {
            query_id: time.time()
        })

    def get(self, query_id):
        timestamp = self.executions.get(str(query_id))
        if timestamp:
            timestamp = utils.dt_from_timestamp(timestamp)

        return timestamp


scheduled_queries_executions = ScheduledQueriesExecutions()


@python_2_unicode_compatible
@generic_repr('id', 'name', 'type', 'org_id', 'created_at', 'folder_id')
class DataSource(BelongsToOrgMixin, db.Model):
    id = Column(db.Integer, primary_key=True)
    org_id = Column(db.Integer, db.ForeignKey('organizations.id'))
    org = db.relationship(Organization, backref="data_sources")
    folder_id = Column(db.Integer, db.ForeignKey('folder_structures.id'), nullable=True, default=None)
    folder = db.relationship("FolderStructure", backref="data_sources")
    name = Column(db.String(255))
    type = Column(db.String(255))
    options = Column('encrypted_options', ConfigurationContainer.as_mutable(
        EncryptedConfiguration(db.Text, settings.DATASOURCE_SECRET_KEY, FernetEngine)))
    queue_name = Column(db.String(255), default="queries")
    scheduled_queue_name = Column(db.String(255), default="scheduled_queries")
    created_at = Column(db.DateTime(True), default=db.func.now())

    data_source_groups = db.relationship("DataSourceGroup", back_populates="data_source",
                                         cascade="all")
    __tablename__ = 'data_sources'
    __table_args__ = (db.Index('data_sources_org_id_name', 'org_id', 'name'),)

    def __eq__(self, other):
        return self.id == other.id

    def to_dict(self, all=False, with_permissions_for=None):
        d = {
            'id': self.id,
            'name': self.name,
            'type': self.type,
            'syntax': self.query_runner.syntax,
            'paused': self.paused,
            'pause_reason': self.pause_reason,
            'folder_id': self.folder_id
        }

        if all:
            schema = get_configuration_schema_for_query_runner_type(self.type)
            self.options.set_schema(schema)
            d['options'] = self.options.to_dict(mask_secrets=True)
            d['queue_name'] = self.queue_name
            d['scheduled_queue_name'] = self.scheduled_queue_name
            d['groups'] = self.groups

        if with_permissions_for is not None:
            d['view_only'] = db.session.query(DataSourceGroup.view_only).filter(
                DataSourceGroup.group == with_permissions_for,
                DataSourceGroup.data_source == self).one()[0]

        return d

    def __str__(self):
        return text_type(self.name)

    @classmethod
    def create_with_group(cls, *args, **kwargs):
        data_source = cls(*args, **kwargs)
        data_source_group = DataSourceGroup(
            data_source=data_source,
            group=data_source.org.admin_group)
        db.session.add_all([data_source, data_source_group])
        return data_source

    @classmethod
    def all(cls, org, group_ids=None):
        data_sources = cls.query.filter(cls.org == org).order_by(cls.id.asc())

        if group_ids:
            data_sources = data_sources.join(DataSourceGroup).filter(
                DataSourceGroup.group_id.in_(group_ids))

        return data_sources.distinct()

    @classmethod
    def get_by_id(cls, _id):
        return cls.query.filter(cls.id == _id).one()

    def delete(self):
        Query.query.filter(Query.data_source == self).update(dict(data_source_id=None, latest_query_data_id=None))
        QueryResult.query.filter(QueryResult.data_source == self).delete()
        res = db.session.delete(self)
        db.session.commit()
        return res

    def get_schema(self, refresh=False, prefix=None):
        key = "data_source:schema:{}".format(self.id)

        cache = None
        if not refresh:
            cache = redis_connection.get(key)

        if cache is None:
            query_runner = self.query_runner
            schema = sorted(query_runner.get_schema(prefix), key=lambda t: t['name'])

            redis_connection.set(key, json_dumps(schema))
        else:
            schema = json_loads(cache)

        return schema

    def _pause_key(self):
        return 'ds:{}:pause'.format(self.id)

    @property
    def paused(self):
        return redis_connection.exists(self._pause_key())

    @property
    def pause_reason(self):
        return redis_connection.get(self._pause_key())

    def pause(self, reason=None):
        redis_connection.set(self._pause_key(), reason or '')

    def resume(self):
        redis_connection.delete(self._pause_key())

    def add_group(self, group, view_only=False):
        dsg = DataSourceGroup(group=group, data_source=self, view_only=view_only)
        db.session.add(dsg)
        return dsg

    def remove_group(self, group):
        DataSourceGroup.query.filter(
            DataSourceGroup.group == group,
            DataSourceGroup.data_source == self
        ).delete()
        db.session.commit()

    def update_group_permission(self, group, view_only):
        dsg = DataSourceGroup.query.filter(
            DataSourceGroup.group == group,
            DataSourceGroup.data_source == self).one()
        dsg.view_only = view_only
        db.session.add(dsg)
        return dsg

    @property
    def query_runner(self):
        return get_query_runner(self.type, self.options)

    @classmethod
    def get_by_name(cls, name):
        return cls.query.filter(cls.name == name).one()

    # XXX examine call sites to see if a regular SQLA collection would work better
    @property
    def groups(self):
        groups = DataSourceGroup.query.filter(
            DataSourceGroup.data_source == self
        )
        return dict(map(lambda g: (g.group_id, g.view_only), groups))

    def update_folder(self, folder_id):
        self.folder_id = folder_id
        db.session.commit()


@generic_repr('id', 'data_source_id', 'group_id', 'view_only')
class DataSourceGroup(db.Model):
    # XXX drop id, use datasource/group as PK
    id = Column(db.Integer, primary_key=True)
    data_source_id = Column(db.Integer, db.ForeignKey("data_sources.id"))
    data_source = db.relationship(DataSource, back_populates="data_source_groups")
    group_id = Column(db.Integer, db.ForeignKey("groups.id"))
    group = db.relationship(Group, back_populates="data_sources")
    view_only = Column(db.Boolean, default=False)

    __tablename__ = "data_source_groups"


@python_2_unicode_compatible
@generic_repr('id', 'org_id', 'data_source_id', 'query_hash', 'runtime', 'retrieved_at')
class QueryResult(db.Model, BelongsToOrgMixin):
    id = Column(db.Integer, primary_key=True)
    org_id = Column(db.Integer, db.ForeignKey('organizations.id'))
    org = db.relationship(Organization)
    data_source_id = Column(db.Integer, db.ForeignKey("data_sources.id"))
    data_source = db.relationship(DataSource, backref=backref('query_results'))
    query_hash = Column(db.String(32), index=True)
    query_text = Column('query', db.Text)
    data = Column(db.Text)
    runtime = Column(postgresql.DOUBLE_PRECISION)
    retrieved_at = Column(db.DateTime(True))

    __tablename__ = 'query_results'

    def __str__(self):
        return u"%d | %s | %s" % (self.id, self.query_hash, self.retrieved_at)

    def to_dict(self):
        return {
            'id': self.id,
            'query_hash': self.query_hash,
            'query': self.query_text,
            'data': json_loads(self.data),
            'data_source_id': self.data_source_id,
            'runtime': self.runtime,
            'retrieved_at': self.retrieved_at
        }

    @classmethod
    def unused(cls, days=7):
        age_threshold = datetime.datetime.now() - datetime.timedelta(days=days)
        return (
            cls.query.filter(
                Query.id.is_(None),
                cls.retrieved_at < age_threshold
            )
                .outerjoin(Query)
        ).options(load_only('id'))

    @classmethod
    def get_latest(cls, data_source, query, max_age=0):
        query_hash = utils.gen_query_hash(query)

        if max_age == -1:
            query = cls.query.filter(
                cls.query_hash == query_hash,
                cls.data_source == data_source
            )
        else:
            query = cls.query.filter(
                cls.query_hash == query_hash,
                cls.data_source == data_source,
                (
                        db.func.timezone('utc', cls.retrieved_at) +
                        datetime.timedelta(seconds=max_age) >=
                        db.func.timezone('utc', db.func.now())
                )
            )

        return query.order_by(cls.retrieved_at.desc()).first()

    @classmethod
    def store_result(cls, org, data_source, query_hash, query, data, run_time, retrieved_at):
        query_result = cls(org_id=org,
                           query_hash=query_hash,
                           query_text=query,
                           runtime=run_time,
                           data_source=data_source,
                           retrieved_at=retrieved_at,
                           data=data)
        db.session.add(query_result)
        logging.info("Inserted query (%s) data; id=%s", query_hash, query_result.id)
        # TODO: Investigate how big an impact this select-before-update makes.
        queries = Query.query.filter(
            Query.query_hash == query_hash,
            Query.data_source == data_source
        )
        for q in queries:
            q.latest_query_data = query_result
            # don't auto-update the updated_at timestamp
            q.skip_updated_at = True
            db.session.add(q)
        query_ids = [q.id for q in queries]
        logging.info("Updated %s queries with result (%s).", len(query_ids), query_hash)

        return query_result, query_ids

    @property
    def groups(self):
        return self.data_source.groups

    def make_csv_content(self):
        s = cStringIO.StringIO()

        query_data = json_loads(self.data)
        writer = csv.DictWriter(s, extrasaction="ignore", fieldnames=[col['name'] for col in query_data['columns']])
        writer.writer = utils.UnicodeWriter(s)
        writer.writeheader()
        for row in query_data['rows']:
            writer.writerow(row)

        return s.getvalue()

    def make_excel_content(self):
        s = cStringIO.StringIO()

        query_data = json_loads(self.data)
        book = xlsxwriter.Workbook(s, {'constant_memory': True})
        sheet = book.add_worksheet("result")

        column_names = []
        for (c, col) in enumerate(query_data['columns']):
            sheet.write(0, c, col['name'])
            column_names.append(col['name'])

        for (r, row) in enumerate(query_data['rows']):
            for (c, name) in enumerate(column_names):
                v = row.get(name)
                if isinstance(v, list) or isinstance(v, dict):
                    v = str(v).encode('utf-8')
                sheet.write(r + 1, c, v)

        book.close()

        return s.getvalue()


def should_schedule_next(previous_iteration, now, interval, time=None, day_of_week=None, failures=0):
    # if time exists then interval > 23 hours (82800s)
    # if day_of_week exists then interval > 6 days (518400s)
    if (time is None):
        ttl = int(interval)
        next_iteration = previous_iteration + datetime.timedelta(seconds=ttl)
    else:
        hour, minute = time.split(':')
        hour, minute = int(hour), int(minute)

        # The following logic is needed for cases like the following:
        # - The query scheduled to run at 23:59.
        # - The scheduler wakes up at 00:01.
        # - Using naive implementation of comparing timestamps, it will skip the execution.
        normalized_previous_iteration = previous_iteration.replace(hour=hour, minute=minute)

        if normalized_previous_iteration > previous_iteration:
            previous_iteration = normalized_previous_iteration - datetime.timedelta(days=1)

        days_delay = int(interval) / 60 / 60 / 24

        days_to_add = 0
        if (day_of_week is not None):
            days_to_add = list(calendar.day_name).index(day_of_week) - normalized_previous_iteration.weekday()

        next_iteration = (previous_iteration + datetime.timedelta(days=days_delay) +
                          datetime.timedelta(days=days_to_add)).replace(hour=hour, minute=minute)
    if failures:
        next_iteration += datetime.timedelta(minutes=2 ** failures)
    return now > next_iteration


@python_2_unicode_compatible
@gfk_type
@generic_repr('id', 'name', 'query_hash', 'version', 'user_id', 'org_id',
              'data_source_id','description','query_hash', 'last_modified_by_id',
              'is_archived', 'is_draft', 'schedule', 'schedule_failures', 'folder_id')
class Query(ChangeTrackingMixin, TimestampMixin, BelongsToOrgMixin, db.Model):
    id = Column(db.Integer, primary_key=True)
    version = Column(db.Integer, default=1)
    org_id = Column(db.Integer, db.ForeignKey('organizations.id'))
    org = db.relationship(Organization, backref="queries")
    folder_id = Column(db.Integer, db.ForeignKey("folder_structures.id"), nullable=True, default=None)
    folder = db.relationship("FolderStructure", backref="queries")
    data_source_id = Column(db.Integer, db.ForeignKey("data_sources.id"), nullable=True)
    data_source = db.relationship(DataSource, backref='queries')
    latest_query_data_id = Column(db.Integer, db.ForeignKey("query_results.id"), nullable=True)
    latest_query_data = db.relationship(QueryResult)
    name = Column(db.String(255))
    description = Column(db.String(4096), nullable=True)
    query_text = Column("query", db.Text)
    query_hash = Column(db.String(32))
    api_key = Column(db.String(40), default=lambda: generate_token(40))
    user_id = Column(db.Integer, db.ForeignKey("users.id"))
    user = db.relationship(User, foreign_keys=[user_id])
    last_modified_by_id = Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    last_modified_by = db.relationship(User, backref="modified_queries",
                                       foreign_keys=[last_modified_by_id])
    is_archived = Column(db.Boolean, default=False, index=True)
    is_draft = Column(db.Boolean, default=True, index=True)
    schedule = Column(MutableDict.as_mutable(PseudoJSON), nullable=True)
    schedule_failures = Column(db.Integer, default=0)
    visualizations = db.relationship("Visualization", cascade="all, delete-orphan")
    options = Column(MutableDict.as_mutable(PseudoJSON), default={})
    search_vector = Column(TSVectorType('id', 'name', 'description', 'query',
                                        weights={'name': 'A',
                                                 'id': 'B',
                                                 'description': 'C',
                                                 'query': 'D'}),
                           nullable=True)
    tags = Column('tags', MutableList.as_mutable(postgresql.ARRAY(db.Unicode)), nullable=True)

    query_groups = db.relationship("QueryGroup", cascade="all, delete-orphan")
    query_class = SearchBaseQuery
    __tablename__ = 'queries'
    __mapper_args__ = {
        "version_id_col": version,
        'version_id_generator': False
    }

    def to_dict(self, all=False, with_permissions_for=None):
        d = {
            'id': self.id,
            'name': self.name,
            'query_text': self.query_text,
            'is_draft': self.is_draft,
            'description': self.description,
            'folder_id': self.folder_id
        }

        if all:
            d['user'] = self.user.to_dict()
            d['data_source'] = self.data_source.to_dict()
            d['visualizations'] = self.visualizations.to_dict()
            d['groups'] = self.query_groups.to_dict()

        if with_permissions_for is not None:
            d['view_only'] = db.session.query(QueryGroup.view_only).filter(
                QueryGroup.group == with_permissions_for,
                QueryGroup.query_rel == self).one()[0]

        return d

    def __str__(self):
        return text_type(self.id)

    @classmethod
    def create_with_group(cls, *args, **kwargs):
        query = cls(*args, **kwargs)
        group = cls(*args, **kwargs)
        query_group = QueryGroup(
            query=query,
            group=group)
        db.session.add_all([query, query_group])
        return query

    def archive(self, user=None):
        db.session.add(self)
        self.is_archived = True
        self.schedule = None

        for vis in self.visualizations:
            for w in vis.widgets:
                db.session.delete(w)
            vis.is_archived = True
            db.session.add(vis)

        for a in self.alerts:
            db.session.delete(a)

        if user:
            self.record_changes(user)

    @classmethod
    def create(cls, **kwargs):
        query = cls(**kwargs)
        db.session.add(Visualization(query_rel=query,
                                     name="Table",
                                     description='',
                                     type="TABLE",
                                     user=kwargs.get('user'),
                                     folder_id=kwargs.get('folder_id'),
                                     options="{}"))
        return query

    @classmethod
    def all_queries(cls, group_ids, user_id=None, include_drafts=False, include_archived=False):
        query_ids = (
            db.session
                .query(distinct(cls.id))
                .join(
                DataSourceGroup,
                Query.data_source_id == DataSourceGroup.data_source_id
            )
                .filter(Query.is_archived.is_(False))
                .filter(DataSourceGroup.group_id.in_(group_ids))
        )
        queries = (
            cls
                .query
                .options(
                joinedload(Query.visualizations),
                joinedload(Query.user),
                joinedload(
                    Query.latest_query_data
                ).load_only(
                    'runtime',
                    'retrieved_at',
                )
            )
                .filter(cls.id.in_(query_ids))
                # Adding outer joins to be able to order by relationship
                .outerjoin(User, User.id == Query.user_id)
                .outerjoin(
                QueryResult,
                QueryResult.id == Query.latest_query_data_id
            )
                .options(
                contains_eager(Query.user),
                contains_eager(Query.latest_query_data),
            )
        )

        if not include_drafts:
            queries = queries.filter(
                or_(
                    Query.is_draft.is_(False),
                    Query.user_id == user_id
                )
            )
        return queries

    @classmethod
    def get_by_query_group(cls, group_ids, user_id=None, include_drafts=False, include_archived=False):

        queries = (
            cls
                .query
                .options(
                joinedload(Query.visualizations),
                joinedload(Query.user),
                joinedload(
                    Query.latest_query_data
                ).load_only(
                    'runtime',
                    'retrieved_at',
                )
            )
                # Adding outer joins to be able to order by relationship
                .outerjoin(User, User.id == Query.user_id)
                .outerjoin(
                    QueryResult,
                    QueryResult.id == Query.latest_query_data_id)
                .outerjoin(QueryGroup, Query.id == QueryGroup.query_id)
                .options(
                contains_eager(Query.user),
                contains_eager(Query.latest_query_data),
            )
        )

        queries = queries.filter(
            QueryGroup.group_id.in_(group_ids) |
            (Query.user_id == user_id)
        )

        queries = queries.filter(
            Query.is_archived.is_(False)
        )

        if not include_drafts:
            queries = queries.filter(
                or_(
                    Query.is_draft.is_(False),
                    Query.user_id == user_id
                )
            )
        return queries

    @classmethod
    def favorites(cls, user, base_query=None):
        if base_query is None:
            base_query = cls.all_queries(user.group_ids, user.id, include_drafts=True)
        return base_query.join((
            Favorite,
            and_(
                Favorite.object_type == u'Query',
                Favorite.object_id == Query.id
            )
        )).filter(Favorite.user_id == user.id)

    @classmethod
    def all_tags(cls, user, include_drafts=False):
        queries = cls.all_queries(
            group_ids=user.group_ids,
            user_id=user.id,
            include_drafts=include_drafts,
        )

        tag_column = func.unnest(cls.tags).label('tag')
        usage_count = func.count(1).label('usage_count')

        query = (
            db.session
                .query(tag_column, usage_count)
                .group_by(tag_column)
                .filter(Query.id.in_(queries.options(load_only('id'))))
                .order_by(usage_count.desc())
        )
        return query

    @classmethod
    def by_user(cls, user):
        return cls.all_queries(user.group_ids, user.id).filter(Query.user == user)

    @classmethod
    def by_api_key(cls, api_key):
        return cls.query.filter(cls.api_key == api_key).one()

    @classmethod
    def outdated_queries(cls):
        queries = (
            Query.query
                .options(joinedload(Query.latest_query_data).load_only('retrieved_at'))
                .filter(Query.schedule.isnot(None))
                .order_by(Query.id)
        )

        now = utils.utcnow()
        outdated_queries = {}
        scheduled_queries_executions.refresh()

        for query in queries:
            if query.schedule['interval'] is None:
                continue

            if query.schedule['until'] is not None:
                schedule_until = pytz.utc.localize(datetime.datetime.strptime(query.schedule['until'], '%Y-%m-%d'))

                if schedule_until <= now:
                    continue

            if query.latest_query_data:
                retrieved_at = query.latest_query_data.retrieved_at
            else:
                retrieved_at = now

            retrieved_at = scheduled_queries_executions.get(query.id) or retrieved_at

            if should_schedule_next(retrieved_at, now, query.schedule['interval'], query.schedule['time'],
                                    query.schedule['day_of_week'], query.schedule_failures):
                key = "{}:{}".format(query.query_hash, query.data_source_id)
                outdated_queries[key] = query

        return outdated_queries.values()

    @classmethod
    def search(cls, term, group_ids, user_id=None, include_drafts=False,
               limit=None, include_archived=False):
        all_queries = cls.all_queries(
            group_ids,
            user_id=user_id,
            include_drafts=include_drafts,
            include_archived=False,
        )
        # sort the result using the weight as defined in the search vector column
        return all_queries.search(term, sort=True).limit(limit)

    @classmethod
    def search_by_user(cls, term, user, limit=None):
        return cls.by_user(user).search(term, sort=True).limit(limit)

    @classmethod
    def recent(cls, group_ids, user_id=None, limit=20):
        query = (cls.query
                 .filter(Event.created_at > (db.func.current_date() - 7))
                 .join(Event, Query.id == Event.object_id.cast(db.Integer))
                 .join(DataSourceGroup, Query.data_source_id == DataSourceGroup.data_source_id)
                 .filter(
            Event.action.in_(['edit', 'execute', 'edit_name',
                              'edit_description', 'view_source']),
            Event.object_id != None,
            Event.object_type == 'query',
            DataSourceGroup.group_id.in_(group_ids),
            or_(Query.is_draft == False, Query.user_id == user_id),
            Query.is_archived == False)
                 .group_by(Event.object_id, Query.id)
                 .order_by(db.desc(db.func.count(0))))

        if user_id:
            query = query.filter(Event.user_id == user_id)

        query = query.limit(limit)

        return query

    @classmethod
    def get_by_id(cls, _id):
        return cls.query.filter(cls.id == _id).one()

    def add_group(self, group, view_only=False):
        dsg = QueryGroup(group=group, query_rel=self, view_only=view_only)
        db.session.add(dsg)
        return dsg

    def remove_group(self, group):
        QueryGroup.query.filter(
            QueryGroup.group == group,
            QueryGroup.query_rel == self
        ).delete()
        db.session.commit()

    def update_group_permission(self, group, view_only):
        dsg = QueryGroup.query.filter(
            QueryGroup.group == group,
            QueryGroup.query_rel == self).one()
        dsg.view_only = view_only
        db.session.add(dsg)
        return dsg

    @classmethod
    def all_groups_for_query_ids(cls, query_ids):
        query = """SELECT group_id, view_only
                   FROM queries
                   JOIN data_source_groups ON queries.data_source_id = data_source_groups.data_source_id
                   WHERE queries.id in :ids"""

        return db.session.execute(query, {'ids': tuple(query_ids)}).fetchall()

    @classmethod
    def all_query_groups_for_query_ids(cls, query_ids):
        query = """SELECT query_id, group_id, view_only
                   FROM queries
                   JOIN query_groups ON queries.id = query_groups.query_id
                   WHERE queries.id in :ids"""

        return db.session.execute(query, {'ids': tuple(query_ids)}).fetchall()

    def fork(self, user):
        forked_list = ['org', 'data_source', 'latest_query_data', 'description',
                       'query_text', 'query_hash', 'options']
        kwargs = {a: getattr(self, a) for a in forked_list}

        # Query.create will add default TABLE visualization, so use constructor to create bare copy of query
        forked_query = Query(name=u'Copy of (#{}) {}'.format(self.id, self.name), user=user, **kwargs)

        for v in self.visualizations:
            forked_v = v.copy()
            forked_v['query_rel'] = forked_query
            fv = Visualization(**forked_v)  # it will magically add it to `forked_query.visualizations`
            db.session.add(fv)

        db.session.add(forked_query)
        return forked_query

    @property
    def runtime(self):
        return self.latest_query_data.runtime

    @property
    def retrieved_at(self):
        return self.latest_query_data.retrieved_at

    @property
    def groups(self):
        if self.data_source is None:
            return {}

        return self.data_source.groups

    @hybrid_property
    def lowercase_name(self):
        "Optional property useful for sorting purposes."
        return self.name.lower()

    @lowercase_name.expression
    def lowercase_name(cls):
        "The SQLAlchemy expression for the property above."
        return func.lower(cls.name)

    @property
    def parameters(self):
        return self.options.get("parameters", [])

    @property
    def parameterized(self):
        return ParameterizedQuery(self.query_text, self.parameters)

    def update_folder(self, folder_id):
        self.folder_id = folder_id
        db.session.commit()

@listens_for(Query.query_text, 'set')
def gen_query_hash(target, val, oldval, initiator):
    target.query_hash = utils.gen_query_hash(val)
    target.schedule_failures = 0


@listens_for(Query.user_id, 'set')
def query_last_modified_by(target, val, oldval, initiator):
    target.last_modified_by_id = val

@generic_repr('id', 'query_id', 'group_id', 'view_only')
class QueryGroup(db.Model):
    # XXX drop id, use query/group as PK
    id = Column(db.Integer, primary_key=True)
    query_id = Column(db.Integer, db.ForeignKey("queries.id"))
    query_rel = db.relationship(Query, back_populates="query_groups")
    group_id = Column(db.Integer, db.ForeignKey("groups.id"))
    group = db.relationship(Group, back_populates="queries")
    view_only = Column(db.Boolean, default=False)

    __tablename__ = "query_groups"

    def to_dict(self, with_permissions_for=None):
        d = {
            'id': self.id,
            'group_id': self.group_id,
            'query_id': self.query_id,
            'view_only':self.view_only
        }

        if with_permissions_for is not None:
            d['group'] = self.group.to_dict()

        return d

    @classmethod
    def get_by_query_group(cls, query, group):
        return cls.query.filter(cls.query_id == query.id, cls.group_id == group.id).one()

    @classmethod
    def get_by_query_groups(cls, query, groups):
        result = cls.query.filter(cls.query_id == query.id, cls.group_id.in_([group.group_id for group in groups]))
        return result

    @classmethod
    def get_by_query(cls, query):
        return cls.query.filter(cls.query_id == query.id)

@generic_repr('id', 'object_type', 'object_id', 'user_id', 'org_id')
class Favorite(TimestampMixin, db.Model):
    id = Column(db.Integer, primary_key=True)
    org_id = Column(db.Integer, db.ForeignKey("organizations.id"))

    object_type = Column(db.Unicode(255))
    object_id = Column(db.Integer)
    object = generic_relationship(object_type, object_id)

    user_id = Column(db.Integer, db.ForeignKey("users.id"))
    user = db.relationship(User, backref='favorites')

    __tablename__ = "favorites"
    __table_args__ = (
        UniqueConstraint("object_type", "object_id", "user_id", name="unique_favorite"),
    )

    @classmethod
    def is_favorite(cls, user, object):
        return cls.query.filter(cls.object == object, cls.user_id == user).count() > 0

    @classmethod
    def are_favorites(cls, user, objects):
        objects = list(objects)
        if not objects:
            return []

        object_type = text_type(objects[0].__class__.__name__)
        return map(lambda fav: fav.object_id,
                   cls.query.filter(cls.object_id.in_(map(lambda o: o.id, objects)), cls.object_type == object_type,
                                    cls.user_id == user))


@generic_repr('id', 'name', 'query_id', 'user_id', 'state', 'last_triggered_at', 'rearm')
class Alert(TimestampMixin, BelongsToOrgMixin, db.Model):
    UNKNOWN_STATE = 'unknown'
    OK_STATE = 'ok'
    TRIGGERED_STATE = 'triggered'

    id = Column(db.Integer, primary_key=True)
    name = Column(db.String(255))
    query_id = Column(db.Integer, db.ForeignKey("queries.id"))
    query_rel = db.relationship(Query, backref=backref('alerts', cascade="all"))
    user_id = Column(db.Integer, db.ForeignKey("users.id"))
    user = db.relationship(User, backref='alerts')
    options = Column(MutableDict.as_mutable(PseudoJSON))
    state = Column(db.String(255), default=UNKNOWN_STATE)
    subscriptions = db.relationship("AlertSubscription", cascade="all, delete-orphan")
    last_triggered_at = Column(db.DateTime(True), nullable=True)
    rearm = Column(db.Integer, nullable=True)

    __tablename__ = 'alerts'

    @classmethod
    def all(cls, group_ids):
        return (
            cls.query
                .options(
                joinedload(Alert.user),
                joinedload(Alert.query_rel),
            )
                .join(Query)
                .join(
                DataSourceGroup,
                DataSourceGroup.data_source_id == Query.data_source_id
            )
                .filter(DataSourceGroup.group_id.in_(group_ids))
        )

    @classmethod
    def get_by_id_and_org(cls, object_id, org):
        return super(Alert, cls).get_by_id_and_org(object_id, org, Query)

    def evaluate(self):
        data = json_loads(self.query_rel.latest_query_data.data)

        if data['rows'] and self.options['column'] in data['rows'][0]:
            value = data['rows'][0][self.options['column']]
            op = self.options['op']

            if op == 'greater than' and value > self.options['value']:
                new_state = self.TRIGGERED_STATE
            elif op == 'less than' and value < self.options['value']:
                new_state = self.TRIGGERED_STATE
            elif op == 'equals' and value == self.options['value']:
                new_state = self.TRIGGERED_STATE
            else:
                new_state = self.OK_STATE
        else:
            new_state = self.UNKNOWN_STATE

        return new_state

    def subscribers(self):
        return User.query.join(AlertSubscription).filter(AlertSubscription.alert == self)

    @property
    def groups(self):
        return self.query_rel.groups


def generate_slug(ctx):
    slug = utils.slugify(ctx.current_parameters['name'])
    tries = 1
    while Dashboard.query.filter(Dashboard.slug == slug).first() is not None:
        slug = utils.slugify(ctx.current_parameters['name']) + "_" + str(tries)
        tries += 1
    return slug


@python_2_unicode_compatible
@gfk_type
@generic_repr('id', 'name', 'slug', 'user_id', 'org_id', 'description', 'type', 'version', 'is_archived', 'is_draft',
              'background_image', 'folder_id')
class Dashboard(ChangeTrackingMixin, TimestampMixin, BelongsToOrgMixin, db.Model):
    id = Column(db.Integer, primary_key=True)
    version = Column(db.Integer)
    org_id = Column(db.Integer, db.ForeignKey("organizations.id"))
    org = db.relationship(Organization, backref="dashboards")
    folder_id = Column(db.Integer, db.ForeignKey("folder_structures.id"), nullable=True, default=None)
    folder = db.relationship("FolderStructure", backref="dashboards")
    slug = Column(db.String(140), index=True, default=generate_slug)
    name = Column(db.String(100))
    user_id = Column(db.Integer, db.ForeignKey("users.id"))
    user = db.relationship(User)
    description = Column(db.String(4096), nullable=True)
    type = Column(db.String(100), nullable=True)
    # layout is no longer used, but kept so we know how to render old dashboards.
    layout = Column(db.Text)
    background_image = Column(db.String(1024), nullable=True)
    dashboard_filters_enabled = Column(db.Boolean, default=False)
    is_archived = Column(db.Boolean, default=False, index=True)
    is_draft = Column(db.Boolean, default=True, index=True)
    widgets = db.relationship('Widget', backref='dashboard', lazy='dynamic')
    tags = Column('tags', MutableList.as_mutable(postgresql.ARRAY(db.Unicode)), nullable=True)

    dashboard_groups = db.relationship("DashboardGroup", back_populates="dashboard",
                                         cascade="all, delete-orphan")

    __tablename__ = 'dashboards'
    __mapper_args__ = {
        "version_id_col": version
    }

    def to_dict(self, all=False, with_permissions_for=None):
        d = {
            'id': self.id,
            'name': self.name,
            'type': self.type,
            'is_draft': self.is_draft,
            'description': self.description,
            'folder_id': self.folder_id
        }

        if all:
            d['user'] = self.user.to_dict()
            d['widgets'] = self.widgets.to_dict()
            d['groups'] = self.dashboard_groups.to_dict()

        if with_permissions_for is not None:
            d['view_only'] = db.session.query(DashboardGroup.view_only).filter(
                DashboardGroup.group == with_permissions_for,
                DashboardGroup.dashboard == self).one()[0]

        return d

    def __str__(self):
        return u"%s=%s" % (self.id, self.name)

    @classmethod
    def create_with_group(cls, *args, **kwargs):
        dashboard = cls(*args, **kwargs)
        group = cls(*args, **kwargs)
        dashboard_group = DashboardGroup(
            dashboard=dashboard,
            group=group)
        db.session.add_all([dashboard, dashboard_group])
        return dashboard


    @classmethod
    def all(cls, org, group_ids, user_id, viz_id=None):

        if has_permission('super_admin'):
            query = (
                Dashboard.query
                    .outerjoin(Widget)
                    .outerjoin(Visualization)
                    .outerjoin(Query)
                    .filter(
                        and_(Dashboard.is_archived == False, Dashboard.org == org)
                    )
                    .distinct())
        else:
            query = (
                Dashboard.query
                    .outerjoin(Widget)
                    .outerjoin(Visualization)
                    .outerjoin(Query)
                    .outerjoin(DataSourceGroup, Query.data_source_id == DataSourceGroup.data_source_id)
                    .filter(
                    Dashboard.is_archived == False, (
                            DataSourceGroup.group_id.in_(group_ids) |
                            ((Widget.dashboard is not None) & (Widget.visualization is None))
                    ),
                    (
                            (viz_id is None) or (Visualization.id == viz_id)
                    ),
                    Dashboard.org == org)
                    .distinct())

        #query = query.filter(or_(Dashboard.user_id == user_id, Dashboard.is_draft == False))
        #logger.debug(query)
        return query

    @classmethod
    def get_by_dashboard_group(cls, org, group_ids, user_id, viz_id=None):
        query = (
            Dashboard.query
                .outerjoin(Widget)
                .outerjoin(Visualization)
                .outerjoin(Query)
                .outerjoin(DashboardGroup, Dashboard.id == DashboardGroup.dashboard_id)
                .filter(
                Dashboard.is_archived == False, (
                        DashboardGroup.group_id.in_(group_ids) |
                        (Dashboard.user_id == user_id) |
                        ((Widget.dashboard is not None) & (Widget.visualization is None))
                ),
                (
                        (viz_id is None) or (Visualization.id == viz_id)
                ),
                Dashboard.org == org)
                .distinct())

        query = query.filter(or_(Dashboard.user_id == user_id, Dashboard.is_draft == False))

        return query

    @classmethod
    def search(cls, org, groups_ids, user_id, search_term, viz_id=None):
        # TODO: switch to FTS
        return cls.all(org, groups_ids, user_id, viz_id).filter(cls.name.ilike(u'%{}%'.format(search_term)))

    @classmethod
    def all_tags(cls, org, user):
        dashboards = cls.all(org, user.group_ids, user.id)

        tag_column = func.unnest(cls.tags).label('tag')
        usage_count = func.count(1).label('usage_count')

        query = (
            db.session
                .query(tag_column, usage_count)
                .group_by(tag_column)
                .filter(Dashboard.id.in_(dashboards.options(load_only('id'))))
                .order_by(usage_count.desc())
        )
        return query

    @classmethod
    def favorites(cls, user, base_query=None):
        if base_query is None:
            base_query = cls.all(user.org, user.group_ids, user.id)
        return base_query.join(
            (
                Favorite,
                and_(
                    Favorite.object_type == u'Dashboard',
                    Favorite.object_id == Dashboard.id
                )
            )
        ).filter(Favorite.user_id == user.id)

    @classmethod
    def get_by_slug_and_org(cls, slug, org):
        return cls.query.filter(cls.slug == slug, cls.org == org).one()

    def add_group(self, group, view_only=False):
        dsg = DashboardGroup(group=group, dashboard=self, view_only=view_only)
        db.session.add(dsg)
        return dsg

    def remove_group(self, group):
        DashboardGroup.query.filter(
            DashboardGroup.group == group,
            DashboardGroup.dashboard == self
        ).delete()
        db.session.commit()

    def update_group_permission(self, group, view_only):
        dsg = DashboardGroup.query.filter(
            DashboardGroup.group == group,
            DashboardGroup.dashboard == self).one()
        dsg.view_only = view_only
        db.session.add(dsg)
        return dsg

    @hybrid_property
    def lowercase_name(self):
        "Optional property useful for sorting purposes."
        return self.name.lower()

    @lowercase_name.expression
    def lowercase_name(cls):
        "The SQLAlchemy expression for the property above."
        return func.lower(cls.name)

    def update_folder(self, folder_id):
        self.folder_id = folder_id
        db.session.commit()



@generic_repr('id', 'dashboard_id', 'group_id', 'view_only')
class DashboardGroup(db.Model):
    # XXX drop id, use datasource/group as PK
    id = Column(db.Integer, primary_key=True)
    dashboard_id = Column(db.Integer, db.ForeignKey("dashboards.id"))
    dashboard = db.relationship(Dashboard, back_populates="dashboard_groups")
    group_id = Column(db.Integer, db.ForeignKey("groups.id"))
    group = db.relationship(Group, back_populates="dashboards")
    view_only = Column(db.Boolean, default=False)

    __tablename__ = "dashboard_groups"

    def to_dict(self, with_permissions_for=None):
        d = {
            'id': self.id,
            'group_id': self.group_id,
            'dashboard_id': self.dashboard_id,
            'view_only':self.view_only
        }

        if with_permissions_for is not None:
            d['group'] = self.group.to_dict()

        return d

    @classmethod
    def get_by_dashboard_group(cls, dshboard, group):
        return cls.query.filter(cls.dashboard_id == dshboard.id, cls.group_id == group.id).one()

    @classmethod
    def get_by_dashboard(cls, dshboard):
        return cls.query.filter(cls.dashboard_id == dshboard.id)

@python_2_unicode_compatible
@gfk_type
@generic_repr('id', 'name', 'type', 'query_id', 'description', 'is_archived', 'version', 'folder_id')
class Visualization(ChangeTrackingMixin, TimestampMixin, BelongsToOrgMixin, db.Model):
    id = Column(db.Integer, primary_key=True)
    version = Column(db.Integer, default=1)
    type = Column(db.String(100))
    query_id = Column(db.Integer, db.ForeignKey("queries.id"))
    # query_rel and not query, because db.Model already has query defined.
    query_rel = db.relationship(Query, back_populates='visualizations')
    folder_id = Column(db.Integer, db.ForeignKey("folder_structures.id"), nullable=True, default=None)
    folder = db.relationship("FolderStructure", backref="visualizations")
    user_id = Column(db.Integer, db.ForeignKey("users.id"))
    user = db.relationship(User)
    name = Column(db.String(255))
    description = Column(db.String(4096), nullable=True)
    is_archived = Column(db.Boolean, default=False, index=True)
    options = Column(db.Text)

    __tablename__ = 'visualizations'

    def __str__(self):
        return u"%s %s" % (self.id, self.type)

    def archive(self, user=None):
        db.session.add(self)
        self.is_archived = True

        for w in self.widgets:
            db.session.delete(w)

        if user:
            self.record_changes(user)

    @hybrid_property
    def lowercase_name(self):
        "Optional property useful for sorting purposes."
        return self.name.lower()

    @lowercase_name.expression
    def lowercase_name(cls):
        "The SQLAlchemy expression for the property above."
        return func.lower(cls.name)

    @classmethod
    def all(cls, group_ids, user_id):
        query = (
            Visualization.query
                .options(
                subqueryload(Visualization.user).load_only('_profile_image_url', 'name'),
            )
                .outerjoin(Query)
                .outerjoin(DataSourceGroup, Query.data_source_id == DataSourceGroup.data_source_id)
                .filter(
                Visualization.is_archived == False,
                (DataSourceGroup.group_id.in_(group_ids) |
                 (Visualization.user_id == user_id)))
                .distinct())

        return query

    @classmethod
    def search(cls, search_term, groups_ids, user_id):
        return cls.all(groups_ids, user_id).filter(cls.name.ilike(u'%{}%'.format(search_term)))

    @classmethod
    def get_by_id(cls, object_id):
        return cls.query.filter(cls.id == object_id).one()

    @classmethod
    def get_by_id_and_org(cls, object_id, org):
        return super(Visualization, cls).get_by_id_and_org(object_id, org, Query)

    def copy(self):
        return {
            'type': self.type,
            'name': self.name,
            'description': self.description,
            'options': self.options
        }

    def update_folder(self, folder_id):
        self.folder_id = folder_id
        db.session.commit()


@python_2_unicode_compatible
@generic_repr('id', 'visualization_id', 'dashboard_id')
class Widget(TimestampMixin, BelongsToOrgMixin, db.Model):
    id = Column(db.Integer, primary_key=True)
    visualization_id = Column(db.Integer, db.ForeignKey('visualizations.id'), nullable=True)
    visualization = db.relationship(Visualization, backref=backref('widgets', cascade='delete'))
    text = Column(db.Text, nullable=True)
    width = Column(db.Integer)
    options = Column(db.Text)
    dashboard_id = Column(db.Integer, db.ForeignKey("dashboards.id"), index=True)

    __tablename__ = 'widgets'

    def __str__(self):
        return u"%s" % self.id

    @classmethod
    def get_by_id_and_org(cls, object_id, org):
        return super(Widget, cls).get_by_id_and_org(object_id, org, Dashboard)


@python_2_unicode_compatible
@generic_repr('id', 'object_type', 'object_id', 'action', 'user_id', 'org_id', 'created_at')
class Event(db.Model):
    id = Column(db.Integer, primary_key=True)
    org_id = Column(db.Integer, db.ForeignKey("organizations.id"))
    org = db.relationship(Organization, back_populates="events")
    user_id = Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    user = db.relationship(User, backref="events")
    action = Column(db.String(255))
    object_type = Column(db.String(255))
    object_id = Column(db.String(255), nullable=True)
    additional_properties = Column(MutableDict.as_mutable(PseudoJSON), nullable=True, default={})
    created_at = Column(db.DateTime(True), default=db.func.now())

    __tablename__ = 'events'

    def __str__(self):
        return u"%s,%s,%s,%s" % (self.user_id, self.action, self.object_type, self.object_id)

    def to_dict(self):
        return {
            'org_id': self.org_id,
            'user_id': self.user_id,
            'action': self.action,
            'object_type': self.object_type,
            'object_id': self.object_id,
            'additional_properties': self.additional_properties,
            'created_at': self.created_at.isoformat()
        }

    @classmethod
    def record(cls, event):
        org_id = event.pop('org_id')
        user_id = event.pop('user_id', None)
        action = event.pop('action')
        object_type = event.pop('object_type')
        object_id = event.pop('object_id', None)

        created_at = datetime.datetime.utcfromtimestamp(event.pop('timestamp'))

        event = cls(org_id=org_id, user_id=user_id, action=action,
                    object_type=object_type, object_id=object_id,
                    additional_properties=event,
                    created_at=created_at)
        db.session.add(event)
        return event


@generic_repr('id', 'created_by_id', 'org_id', 'active')
class ApiKey(TimestampMixin, GFKBase, db.Model):
    id = Column(db.Integer, primary_key=True)
    org_id = Column(db.Integer, db.ForeignKey("organizations.id"))
    org = db.relationship(Organization)
    api_key = Column(db.String(255), index=True, default=lambda: generate_token(40))
    active = Column(db.Boolean, default=True)
    # 'object' provided by GFKBase
    created_by_id = Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_by = db.relationship(User)

    __tablename__ = 'api_keys'
    __table_args__ = (
        db.Index('api_keys_object_type_object_id', 'object_type', 'object_id'),
    )

    @classmethod
    def get_by_api_key(cls, api_key):
        return cls.query.filter(cls.api_key == api_key, cls.active == True).one()

    @classmethod
    def get_by_object(cls, object):
        return cls.query.filter(
            cls.object_type == object.__class__.__tablename__,
            cls.object_id == object.id,
            cls.active == True
        ).first()

    @classmethod
    def create_for_object(cls, object, user):
        k = cls(org=user.org, object=object, created_by=user)
        db.session.add(k)
        return k


@python_2_unicode_compatible
@generic_repr('id', 'name', 'type', 'user_id', 'org_id', 'created_at')
class NotificationDestination(BelongsToOrgMixin, db.Model):
    id = Column(db.Integer, primary_key=True)
    org_id = Column(db.Integer, db.ForeignKey("organizations.id"))
    org = db.relationship(Organization, backref="notification_destinations")
    user_id = Column(db.Integer, db.ForeignKey("users.id"))
    user = db.relationship(User, backref="notification_destinations")
    name = Column(db.String(255))
    type = Column(db.String(255))
    options = Column(ConfigurationContainer.as_mutable(Configuration))
    created_at = Column(db.DateTime(True), default=db.func.now())

    __tablename__ = 'notification_destinations'
    __table_args__ = (
        db.Index(
            'notification_destinations_org_id_name', 'org_id', 'name', unique=True
        ),
    )

    def __str__(self):
        return text_type(self.name)

    def to_dict(self, all=False):
        d = {
            'id': self.id,
            'name': self.name,
            'type': self.type,
            'icon': self.destination.icon()
        }

        if all:
            schema = get_configuration_schema_for_destination_type(self.type)
            self.options.set_schema(schema)
            d['options'] = self.options.to_dict(mask_secrets=True)

        return d

    @property
    def destination(self):
        return get_destination(self.type, self.options)

    @classmethod
    def all(cls, org):
        notification_destinations = cls.query.filter(cls.org == org).order_by(cls.id.asc())

        return notification_destinations

    def notify(self, alert, query, user, new_state, app, host):
        schema = get_configuration_schema_for_destination_type(self.type)
        self.options.set_schema(schema)
        return self.destination.notify(alert, query, user, new_state,
                                       app, host, self.options)


@generic_repr('id', 'user_id', 'destination_id', 'alert_id')
class AlertSubscription(TimestampMixin, db.Model):
    id = Column(db.Integer, primary_key=True)
    user_id = Column(db.Integer, db.ForeignKey("users.id"))
    user = db.relationship(User)
    destination_id = Column(db.Integer,
                            db.ForeignKey("notification_destinations.id"),
                            nullable=True)
    destination = db.relationship(NotificationDestination)
    alert_id = Column(db.Integer, db.ForeignKey("alerts.id"))
    alert = db.relationship(Alert, back_populates="subscriptions")

    __tablename__ = 'alert_subscriptions'
    __table_args__ = (
        db.Index(
            'alert_subscriptions_destination_id_alert_id',
            'destination_id', 'alert_id', unique=True
        ),
    )

    def to_dict(self):
        d = {
            'id': self.id,
            'user': self.user.to_dict(),
            'alert_id': self.alert_id
        }

        if self.destination:
            d['destination'] = self.destination.to_dict()

        return d

    @classmethod
    def all(cls, alert_id):
        return AlertSubscription.query.join(User).filter(AlertSubscription.alert_id == alert_id)

    def notify(self, alert, query, user, new_state, app, host):
        if self.destination:
            return self.destination.notify(alert, query, user, new_state,
                                           app, host)
        else:
            # User email subscription, so create an email destination object
            config = {'addresses': self.user.email}
            schema = get_configuration_schema_for_destination_type('email')
            options = ConfigurationContainer(config, schema)
            destination = get_destination('email', options)
            return destination.notify(alert, query, user, new_state, app, host, options)


@generic_repr('id', 'trigger', 'user_id', 'org_id')
class QuerySnippet(TimestampMixin, db.Model, BelongsToOrgMixin):
    id = Column(db.Integer, primary_key=True)
    org_id = Column(db.Integer, db.ForeignKey("organizations.id"))
    org = db.relationship(Organization, backref="query_snippets")
    trigger = Column(db.String(255), unique=True)
    description = Column(db.Text)
    user_id = Column(db.Integer, db.ForeignKey("users.id"))
    user = db.relationship(User, backref="query_snippets")
    snippet = Column(db.Text)

    __tablename__ = 'query_snippets'

    @classmethod
    def all(cls, org):
        return cls.query.filter(cls.org == org)

    def to_dict(self):
        d = {
            'id': self.id,
            'trigger': self.trigger,
            'description': self.description,
            'snippet': self.snippet,
            'user': self.user.to_dict(),
            'updated_at': self.updated_at,
            'created_at': self.created_at
        }

        return d

@generic_repr('id', 'parent_id', 'name', 'catalog')
class FolderStructure(db.Model):
    id = Column(db.Integer, primary_key=True)
    parent_id = Column(db.Integer, db.ForeignKey("folder_structures.id"), nullable=True)
    parent_rel = db.relationship("FolderStructure", cascade="all, delete-orphan")
    name = Column(db.String(255), nullable=False, default="New Folder")
    catalog = Column(db.String(255), nullable=False)

    __tablename__ = 'folder_structures'

    @classmethod
    def get_by_id(cls, structure_id):
        return cls.query.filter(cls.id == structure_id).one()

    @classmethod
    def all(cls):
        return cls.query.order_by(cls.id.asc())

    def to_dict(self):
        d = {
            'id': self.id,
            'parent_id': self.parent_id,
            'name': self.name,
            'catalog': self.catalog
        }

        return d

    def update_name(self, name):
        self.name = name
        db.session.commit()

def init_db():
    default_org = Organization(name="Default", slug='default', settings={})
    admin_group = Group(name='admin', permissions=['admin', 'super_admin'], org=default_org, type=Group.BUILTIN_GROUP)

    db.session.add_all([default_org, admin_group])
    # XXX remove after fixing User.group_ids
    db.session.commit()
    return default_org, admin_group
