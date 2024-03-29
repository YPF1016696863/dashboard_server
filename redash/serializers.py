"""
This will eventually replace all the `to_dict` methods of the different model
classes we have. This will ensure cleaner code and better
separation of concerns.
"""
from flask_login import current_user
from funcy import project

from redash import models
from redash.permissions import has_access, view_only
from redash.utils import json_loads


def public_visualization(visualization):
    return {
        'type': visualization.type,
        'name': visualization.name,
        'description': visualization.description,
        'options': json_loads(visualization.options),
        'updated_at': visualization.updated_at,
        'created_at': visualization.created_at,
        'query': serialize_query(visualization.query_rel, with_user=False, with_last_modified_by=False, with_stats=False, with_visualizations=False)
    }


def public_widget(widget):
    res = {
        'id': widget.id,
        'width': widget.width,
        'options': json_loads(widget.options),
        'text': widget.text,
        'updated_at': widget.updated_at,
        'created_at': widget.created_at
    }

    if widget.visualization and widget.visualization.id:
        res['visualization'] = public_visualization(widget.visualization)

    return res


def public_dashboard(dashboard):
    dashboard_dict = project(serialize_dashboard(dashboard, with_favorite_state=False), (
        'name', 'layout', 'dashboard_filters_enabled', 'updated_at',
        'created_at', 'background_image', 'description', 'type'
    ))

    widget_list = (models.Widget.query
                   .filter(models.Widget.dashboard_id == dashboard.id)
                   .outerjoin(models.Visualization)
                   .outerjoin(models.Query))

    dashboard_dict['widgets'] = [public_widget(w) for w in widget_list]
    return dashboard_dict


class Serializer(object):
    pass


class QuerySerializer(Serializer):
    def __init__(self, object_or_list, **kwargs):
        self.object_or_list = object_or_list
        self.options = kwargs

    def serialize(self):
        if isinstance(self.object_or_list, models.Query):
            result = serialize_query(self.object_or_list, **self.options)
            if self.options.get('with_favorite_state', True) and not current_user.is_api_user():
                result['is_favorite'] = models.Favorite.is_favorite(current_user.id, self.object_or_list)
        else:
            result = [serialize_query(query, **self.options) for query in self.object_or_list]
            if self.options.get('with_favorite_state', True):
                favorite_ids = models.Favorite.are_favorites(current_user.id, self.object_or_list)
                for query in result:
                    query['is_favorite'] = query['id'] in favorite_ids

        return result


def serialize_query(query, with_stats=False, with_visualizations=False, with_user=True, with_last_modified_by=True):
    d = {
        'id': query.id,
        'latest_query_data_id': query.latest_query_data_id,
        'name': query.name,
        'description': query.description,
        'query': query.query_text,
        'query_hash': query.query_hash,
        'schedule': query.schedule,
        'is_archived': query.is_archived,
        'is_draft': query.is_draft,
        'updated_at': query.updated_at,
        'created_at': query.created_at,
        'data_source_id': query.data_source_id,
        'options': query.options,
        'version': query.version,
        'tags': query.tags or [],
        'is_safe': query.parameterized.is_safe,
        'user_id': current_user.id,
        'created_by': query.user.to_dict(),
        'user': current_user.to_dict(),
        'folder_id': query.folder_id
    }

    if with_user:
        d['api_key'] = query.api_key
    #else:
    #    d['user_id'] = query.user_id

    d['groups'] = [g.to_dict(with_permissions_for=True) for g in models.QueryGroup.get_by_query(query)]
    
    if with_last_modified_by:
        d['last_modified_by'] = query.last_modified_by.to_dict() if query.last_modified_by is not None else None
    else:
        d['last_modified_by_id'] = query.last_modified_by_id

    if with_stats:
        if query.latest_query_data is not None:
            d['retrieved_at'] = query.retrieved_at
            d['runtime'] = query.runtime
        else:
            d['retrieved_at'] = None
            d['runtime'] = None

    if with_visualizations:
        d['visualizations'] = []
        for vis in query.visualizations:
            if vis.is_archived == False:
                d['visualizations'].append(serialize_visualization(vis, with_query=False))

    return d


def serialize_visualization(object, with_query=True):
    d = {
        'id': object.id,
        'type': object.type,
        'name': object.name,
        'description': object.description,
        'options': json_loads(object.options),
        'updated_at': object.updated_at,
        'created_at': object.created_at,
        'folder_id': object.folder_id
    }

    if with_query:
        d['query'] = serialize_query(object.query_rel)

    return d


def serialize_widget(object):
    d = {
        'id': object.id,
        'width': object.width,
        'options': json_loads(object.options),
        'dashboard_id': object.dashboard_id,
        'text': object.text,
        'updated_at': object.updated_at,
        'created_at': object.created_at
    }

    if object.visualization and object.visualization.id:
        d['visualization'] = serialize_visualization(object.visualization)

    return d


def serialize_alert(alert, full=True):
    d = {
        'id': alert.id,
        'name': alert.name,
        'options': alert.options,
        'state': alert.state,
        'last_triggered_at': alert.last_triggered_at,
        'updated_at': alert.updated_at,
        'created_at': alert.created_at,
        'rearm': alert.rearm
    }

    if full:
        d['query'] = serialize_query(alert.query_rel)
        d['user'] = alert.user.to_dict()
    else:
        d['query_id'] = alert.query_id
        d['user_id'] = alert.user_id

    return d


def serialize_dashboard(obj, with_widgets=False, user=None, with_favorite_state=True):
    layout = json_loads(obj.layout)

    widgets = []

    if with_widgets:
        for w in obj.widgets:
            if w.visualization_id is None:
                widgets.append(serialize_widget(w))
            elif user and has_access(w.visualization.query_rel, user, view_only):
                widgets.append(serialize_widget(w))
            else:
                widget = project(serialize_widget(w),
                                 ('id', 'width', 'dashboard_id', 'options', 'created_at', 'updated_at'))
                widget['restricted'] = True
                widgets.append(widget)
    else:
        widgets = None

    d = {
        'id': obj.id,
        'slug': obj.slug,
        'name': obj.name,
        'user_id': current_user.id,
        'created_by': obj.user.to_dict(),
        'user': current_user.to_dict(),
        'layout': layout,
        'dashboard_filters_enabled': obj.dashboard_filters_enabled,
        'widgets': widgets,
        'is_archived': obj.is_archived,
        'is_draft': obj.is_draft,
        'tags': obj.tags or [],
        'updated_at': obj.updated_at,
        'created_at': obj.created_at,
        'version': obj.version,
        'background_image': obj.background_image,
        'description': obj.description,
        'type': obj.type or 'dashboard',
        'folder_id' : obj.folder_id
    }

    d['groups'] = [g.to_dict(with_permissions_for=True) for g in models.DashboardGroup.get_by_dashboard(obj)]

    return d


def serialize_dashboard_overview(obj, user=None):
    layout = json_loads(obj.layout)

    visualizations = []

    for w in obj.widgets:
        if w.visualization_id is not None:
            if user and has_access(w.visualization.query_rel, user, view_only):
                visualizations.append(w.visualization_id)

    d = {
        'id': obj.id,
        'slug': obj.slug,
        'name': obj.name,
        'visualizations': visualizations,
        'updated_at': obj.updated_at,
        'created_at': obj.created_at,
        'type': obj.type or 'dashboard'
    }

    return d
