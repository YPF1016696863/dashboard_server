from flask import request
from funcy import partial

from redash import models
from redash.apis.handlers.base import (BaseResource, get_object_or_404,
                                       paginate, order_results as _order_results)
from redash.permissions import (require_object_modify_permission,
                                require_permission, require_access, view_only, can_modify)
from redash.serializers import serialize_visualization
from redash.utils import json_dumps

# Ordering map for relationships
order_map = {
    'name': 'lowercase_name',
    '-name': '-lowercase_name',
    'created_at': 'created_at',
    '-created_at': '-created_at'
}

order_results = partial(
    _order_results,
    default_order='-created_at',
    allowed_orders=order_map,
)


class BaseVisualizationListResource(BaseResource):

    def get_visualizations(self, search_term):
        if search_term:
            results = models.Visualization.search(
                search_term,
                self.current_user.group_ids,
                self.current_user.id,
            )
        else:
            results = models.Visualization.all(
                self.current_user.group_ids,
                self.current_user.id,
            )
        return results

    @require_permission('view_query')
    def get(self):
        """
        Retrieve a list of visualizations.

        :qparam number page_size: Number of visualizations to return per page
        :qparam number page: Page number to retrieve
        :qparam number order: Name of column to order by
        :qparam number q: Full text search term

        Responds with an array of :ref:`visualization <visualization-response-label>` objects.
        """
        search_term = request.args.get('q', '')

        results = self.get_visualizations(search_term)

        # order results according to passed order parameter,
        # special-casing search queries where the database
        # provides an order by search rank
        ordered_results = order_results(results, fallback=not bool(search_term))

        if request.args.has_key('all'):
            response = [serialize_visualization(result) for result in ordered_results]
        else:
            page = request.args.get('page', 1, type=int)
            page_size = request.args.get('page_size', 25, type=int)

            response = paginate(
                ordered_results,
                page=page,
                page_size=page_size,
                serializer=serialize_visualization
            )

        if search_term:
            self.record_event({
                'action': 'search',
                'object_type': 'visualization',
                'term': search_term,
            })
        else:
            self.record_event({
                'action': 'list',
                'object_type': 'visualization',
            })

        return response


class VisualizationListResource(BaseVisualizationListResource):
    @require_permission('edit_query')
    def post(self):
        kwargs = request.get_json(force=True)

        query = get_object_or_404(models.Query.get_by_id_and_org, kwargs.pop('query_id'), self.current_org)
        require_object_modify_permission(query, self.current_user)

        kwargs['options'] = json_dumps(kwargs['options'])
        kwargs['query_rel'] = query
        kwargs['user'] = self.current_user

        vis = models.Visualization(**kwargs)
        models.db.session.add(vis)
        models.db.session.commit()
        return serialize_visualization(vis, with_query=False)


class VisualizationResource(BaseResource):
    @require_permission('view_query')
    def get(self, visualization_id):
        """
        Retrieve a visualization.

        :param visualization_id: ID of visualization to fetch

        Responds with the :ref:`visualization <visualization-response-label>` contents.
        """
        vis = get_object_or_404(models.Visualization.get_by_id, visualization_id)
        require_access(vis.query_rel, self.current_user, view_only)

        result = serialize_visualization(vis, True)
        result['can_edit'] = can_modify(vis, self.current_user)

        self.record_event({
            'action': 'view',
            'object_id': visualization_id,
            'object_type': 'query',
        })

        return result

    @require_permission('edit_query')
    def post(self, visualization_id):
        vis = get_object_or_404(models.Visualization.get_by_id_and_org, visualization_id, self.current_org)
        require_object_modify_permission(vis.query_rel, self.current_user)

        kwargs = request.get_json(force=True)
        if 'options' in kwargs:
            kwargs['options'] = json_dumps(kwargs['options'])

        kwargs.pop('id', None)
        kwargs.pop('query_id', None)

        self.update_model(vis, kwargs)
        d = serialize_visualization(vis, with_query=False)
        models.db.session.commit()
        return d

    @require_permission('edit_query')
    def delete(self, visualization_id):
        """
        Archives a visualization.

        :param visualization_id: ID of the visualization to archive
        """
        vis = get_object_or_404(models.Visualization.get_by_id_and_org, visualization_id, self.current_org)
        require_object_modify_permission(vis.query_rel, self.current_user)
        vis.archive(self.current_user)
        models.db.session.commit()