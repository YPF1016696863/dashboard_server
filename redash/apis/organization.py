from flask_login import current_user, login_required

from redash import models
from redash.apis import routes, json_response
from redash.utils.org_resolving import current_org


@routes.route('/api/organization/status', methods=['GET'])
@login_required
def organization_status():
    counters = {
        'users': models.User.all(current_org).count(),
        'alerts': models.Alert.all(group_ids=current_user.group_ids).count(),
        'data_sources': models.DataSource.all(current_org, group_ids=current_user.group_ids).count(),
        'queries': models.Query.all_queries(current_user.group_ids, current_user.id, include_drafts=True).count(),
        'dashboards': models.Dashboard.query.filter(models.Dashboard.org == current_org,
                                                    models.Dashboard.is_archived == False).count(),
    }

    return json_response(dict(object_counters=counters))
