from flask import request
from flask_restful import abort

from redash import models
from redash.apis.handlers.base import BaseResource, get_object_or_404
from redash.permissions import require_admin, require_permission
import logging

logger = logging.getLogger(__name__)

class GroupListResource(BaseResource):
    @require_admin
    def post(self):
        name = request.json['name']
        isAdmin = request.json['isAdmin']
        permission = models.Group.DEFAULT_PERMISSIONS

        if isAdmin == True:
            permission = models.Group.REGULAR_ADMIN_PERMISSIONS

        group = models.Group(name=name, permissions=permission, org=self.current_org, type=models.Group.REGULAR_GROUP)

        models.db.session.add(group)
        models.db.session.commit()

        self.record_event({
            'action': 'create',
            'object_id': group.id,
            'object_type': 'group'
        })

        return group.to_dict()

    def get(self):
        if self.current_user.has_permission('admin'):
            pfilter = request.args.get('filter')
            if pfilter is not None:
                groups = models.Group.nonAdminGroup()
            else:
                groups = models.Group.all(self.current_org)
        else:
            groups = models.Group.query.filter(
                models.Group.id.in_(self.current_user.group_ids))

        self.record_event({
            'action': 'list',
            'object_id': 'groups',
            'object_type': 'group',
        })

        return [g.to_dict() for g in groups]


class GroupResource(BaseResource):
    @require_admin
    def post(self, group_id):
        group = models.Group.get_by_id_and_org(group_id, self.current_org)

        if group.type == models.Group.BUILTIN_GROUP:
            abort(400, message="Can't modify built-in groups.")

        group.name = request.json['name']
        models.db.session.commit()

        self.record_event({
            'action': 'edit',
            'object_id': group.id,
            'object_type': 'group'
        })

        return group.to_dict()

    def get(self, group_id):
        if not (self.current_user.has_permission('admin') or int(group_id) in self.current_user.group_ids):
            abort(403)

        group = models.Group.get_by_id_and_org(group_id, self.current_org)

        self.record_event({
            'action': 'view',
            'object_id': group_id,
            'object_type': 'group',
        })

        return group.to_dict()

    @require_admin
    def delete(self, group_id):
        group = models.Group.get_by_id_and_org(group_id, self.current_org)
        if group.type == models.Group.BUILTIN_GROUP:
            abort(400, message="Can't delete built-in groups.")

        members = models.Group.members(group_id)
        for member in members:
            member.group_ids.remove(int(group_id))
            models.db.session.add(member)

        models.db.session.delete(group)
        models.db.session.commit()


class GroupMemberListResource(BaseResource):
    @require_admin
    def post(self, group_id):
        user_id = request.json['user_id']
        user = models.User.get_by_id_and_org(user_id, self.current_org)
        group = models.Group.get_by_id_and_org(group_id, self.current_org)
        user.group_ids.append(group.id)
        models.db.session.commit()

        self.record_event({
            'action': 'add_member',
            'object_id': group.id,
            'object_type': 'group',
            'member_id': user.id
        })
        return user.to_dict()

    @require_permission('list_users')
    def get(self, group_id):
        if not (self.current_user.has_permission('admin') or int(group_id) in self.current_user.group_ids):
            abort(403)

        members = models.Group.members(group_id)
        return [m.to_dict() for m in members]


class GroupMemberResource(BaseResource):
    @require_admin
    def delete(self, group_id, user_id):
        user = models.User.get_by_id_and_org(user_id, self.current_org)
        user.group_ids.remove(int(group_id))
        models.db.session.commit()

        self.record_event({
            'action': 'remove_member',
            'object_id': group_id,
            'object_type': 'group',
            'member_id': user.id
        })


def serialize_data_source_with_group(data_source, data_source_group):
    d = data_source.to_dict()
    d['view_only'] = data_source_group.view_only
    return d

class GroupDataSourceListResource(BaseResource):
    @require_admin
    def post(self, group_id):
        data_source_id = request.json['data_source_id']
        data_source = models.DataSource.get_by_id_and_org(data_source_id, self.current_org)
        group = models.Group.get_by_id_and_org(group_id, self.current_org)

        data_source_group = data_source.add_group(group)
        models.db.session.commit()

        self.record_event({
            'action': 'add_data_source',
            'object_id': group_id,
            'object_type': 'group',
            'member_id': data_source.id
        })

        return serialize_data_source_with_group(data_source, data_source_group)

    @require_admin
    def get(self, group_id):
        group = get_object_or_404(models.Group.get_by_id_and_org, group_id,
                                  self.current_org)

        # TOOD: move to models
        data_sources = (models.DataSource.query
                        .join(models.DataSourceGroup)
                        .filter(models.DataSourceGroup.group == group))

        self.record_event({
            'action': 'list',
            'object_id': group_id,
            'object_type': 'group',
        })

        return [ds.to_dict(with_permissions_for=group) for ds in data_sources]


class GroupDataSourceResource(BaseResource):
    @require_admin
    def post(self, group_id, data_source_id):
        data_source = models.DataSource.get_by_id_and_org(data_source_id, self.current_org)
        group = models.Group.get_by_id_and_org(group_id, self.current_org)
        view_only = request.json['view_only']

        data_source_group = data_source.update_group_permission(group, view_only)
        models.db.session.commit()

        self.record_event({
            'action': 'change_data_source_permission',
            'object_id': group_id,
            'object_type': 'group',
            'member_id': data_source.id,
            'view_only': view_only
        })

        return serialize_data_source_with_group(data_source, data_source_group)

    @require_admin
    def delete(self, group_id, data_source_id):
        data_source = models.DataSource.get_by_id_and_org(data_source_id, self.current_org)
        group = models.Group.get_by_id_and_org(group_id, self.current_org)

        data_source.remove_group(group)
        models.db.session.commit()

        self.record_event({
            'action': 'remove_data_source',
            'object_id': group_id,
            'object_type': 'group',
            'member_id': data_source.id
        })


def serialize_dashboard_with_group(dashboard, dashboard_group):
    d = dashboard.to_dict()
    d['view_only'] = dashboard_group.view_only
    return d

def serialize_groups_with_viewonly_for_dashboard(group, dashboard_group):
    d = group.to_dict()
    d['view_only'] = dashboard_group.view_only
    return d

class GroupDashboardListResource(BaseResource):
    @require_admin
    def post(self, group_id):
        dashboard_id = request.json['dashboard_id']
        dashboard = models.Dashboard.get_by_id_and_org(dashboard_id, self.current_org)
        group = models.Group.get_by_id_and_org(group_id, self.current_org)

        dashboard_group = dashboard.add_group(group)
        models.db.session.commit()

        self.record_event({
            'action': 'add_dashboard_group',
            'object_id': group_id,
            'object_type': 'group',
            'member_id': dashboard.id
        })

        return serialize_dashboard_with_group(dashboard, dashboard_group)

    @require_admin
    def get(self, group_id):
        group = get_object_or_404(models.Group.get_by_id_and_org, group_id,
                                  self.current_org)

        # TOOD: move to models
        dashboards = (models.Dashboard.query
                        .join(models.DashboardGroup)
                        .filter(models.DashboardGroup.group == group))

        self.record_event({
            'action': 'list',
            'object_id': group_id,
            'object_type': 'group',
        })

        return [dshboard.to_dict(with_permissions_for=group) for dshboard in dashboards]

class GroupDashboardResource(BaseResource):
    @require_admin
    def post(self, group_id, dashboard_id):
        dashboard = models.Dashboard.get_by_id_and_org(dashboard_id, self.current_org)
        group = models.Group.get_by_id_and_org(group_id, self.current_org)
        view_only = request.json['view_only']

        dashboard_group = dashboard.update_group_permission(group, view_only)
        models.db.session.commit()

        self.record_event({
            'action': 'change_dashboard_permission',
            'object_id': group_id,
            'object_type': 'group',
            'member_id': dashboard.id,
            'view_only': view_only
        })

        return serialize_dashboard_with_group(dashboard, dashboard_group)

    @require_admin
    def delete(self, group_id, dashboard_id):
        dashboard = models.Dashboard.get_by_id_and_org(dashboard_id, self.current_org)
        group = models.Group.get_by_id_and_org(group_id, self.current_org)

        dashboard.remove_group(group)
        models.db.session.commit()

        self.record_event({
            'action': 'remove_dashboard',
            'object_id': group_id,
            'object_type': 'group',
            'member_id': dashboard.id
        })

class GroupDashboardListDashboardResource(BaseResource):
    def get(self, dashboard_id):
        dashboard = get_object_or_404(models.Dashboard.get_by_id_and_org, dashboard_id,
                                  self.current_org)

        # TOOD: move to models
        groups = (models.Group.query
                        .join(models.DashboardGroup)
                        .filter(models.DashboardGroup.dashboard == dashboard))

        self.record_event({
            'action': 'list',
            'object_id': dashboard_id,
            'object_type': 'group',
        })

        return [serialize_groups_with_viewonly_for_dashboard(g, models.DashboardGroup.get_by_dashboard_group(dashboard, g)) for g in groups]

def serialize_query_with_group(query, query_group):
    d = query.to_dict()
    d['view_only'] = query_group.view_only
    return d

def serialize_groups_with_viewonly_for_query(group, query_group):
    d = group.to_dict()
    d['view_only'] = query_group.view_only
    return d

class GroupQueryListResource(BaseResource):
    @require_admin
    def post(self, group_id):
        query_id = request.json['query_id']
        
        query = models.Query.get_by_id_and_org(query_id, self.current_org)
        group = models.Group.get_by_id_and_org(group_id, self.current_org)

        query_group = query.add_group(group)
        models.db.session.commit()

        self.record_event({
            'action': 'add_query_group',
            'object_id': group_id,
            'object_type': 'group',
            'member_id': query.id
        })

        return serialize_query_with_group(query, query_group)

    @require_admin
    def get(self, group_id):
        group = get_object_or_404(models.Group.get_by_id_and_org, group_id,
                                  self.current_org)

        # TOOD: move to models
        queries = (models.Query.query
                        .join(models.QueryGroup)
                        .filter(models.QueryGroup.group == group))

        self.record_event({
            'action': 'list',
            'object_id': group_id,
            'object_type': 'group',
        })

        return [query.to_dict(with_permissions_for=group) for query in queries]

class GroupQueryResource(BaseResource):
    @require_admin
    def post(self, group_id, query_id):
        query = models.Query.get_by_id_and_org(query_id, self.current_org)
        group = models.Group.get_by_id_and_org(group_id, self.current_org)
        view_only = request.json['view_only']

        query_group = query.update_group_permission(group, view_only)
        models.db.session.commit()

        self.record_event({
            'action': 'change_query_permission',
            'object_id': group_id,
            'object_type': 'group',
            'member_id': query.id,
            'view_only': view_only
        })

        return serialize_query_with_group(query, query_group)

    @require_admin
    def delete(self, group_id, query_id):
        query = models.Query.get_by_id_and_org(query_id, self.current_org)
        group = models.Group.get_by_id_and_org(group_id, self.current_org)

        query.remove_group(group)
        models.db.session.commit()

        self.record_event({
            'action': 'remove_query',
            'object_id': group_id,
            'object_type': 'group',
            'member_id': query.id
        })

class GroupQueryListQueryResource(BaseResource):
    def get(self, query_id):
        query = get_object_or_404(models.Query.get_by_id_and_org, query_id,
                                  self.current_org)

        # TOOD: move to models
        groups = (models.Group.query
                        .join(models.QueryGroup)
                        .filter(models.QueryGroup.query_rel == query))

        self.record_event({
            'action': 'list',
            'object_id': query_id,
            'object_type': 'group',
        })

        return [serialize_groups_with_viewonly_for_query(g, models.QueryGroup.get_by_query_group(query, g)) for g in groups]

