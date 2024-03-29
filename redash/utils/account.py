import logging

from flask import render_template
# noinspection PyUnresolvedReferences
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature

from redash import settings
from redash.tasks import send_mail
from redash.utils import base_url

logger = logging.getLogger(__name__)
serializer = URLSafeTimedSerializer(settings.SECRET_KEY)


def invite_token(user):
    return serializer.dumps(str(user.id))


def verify_link_for_user(user):
    token = invite_token(user)
    verify_url = "/verify/{}".format(token)

    return verify_url


def invite_link_for_user(user):
    token = invite_token(user)
    invite_url = "/invite/{}".format(token)

    return invite_url


def reset_link_for_user(user):
    token = invite_token(user)
    invite_url = "/reset/{}".format(token)

    return invite_url


def validate_token(token):
    max_token_age = settings.INVITATION_TOKEN_MAX_AGE
    return serializer.loads(token, max_age=max_token_age)


def send_verify_email(user, verify_url = None):
    if verify_url is None:
        verify_url = verify_link_for_user(user)

    context = {
        'user': user,
        'verify_url': verify_url,
    }

    html_content = render_template('emails/verify.html', **context)
    text_content = render_template('emails/verify.txt', **context)
    subject = u"{}, please verify your email address".format(user.name)

    send_mail.delay([user.email], subject, html_content, text_content)


def send_invite_email(inviter, invited, invite_url = None):
    if invite_url is None:
        invite_url = invite_link_for_user(invited)

    context = {
        'inviter': inviter,
        'invited': invited,
        'invite_url': invite_url,
    }

    html_content = render_template('emails/invite.html', **context)
    text_content = render_template('emails/invite.txt', **context)
    subject = u"{} invited you to join Redash".format(inviter.name)

    send_mail.delay([invited.email], subject, html_content, text_content)


def send_password_reset_email(user, reset_link):
    if reset_link is None:
        reset_link = reset_link_for_user(user)

    context = {
        'user': user,
        'reset_link': reset_link,
    }

    html_content = render_template('emails/reset.html', **context)
    text_content = render_template('emails/reset.txt', **context)
    subject = u"Reset your password"

    send_mail.delay([user.email], subject, html_content, text_content)
    return reset_link
