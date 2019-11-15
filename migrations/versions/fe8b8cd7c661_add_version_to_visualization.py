"""Add version to visualization

Revision ID: fe8b8cd7c661
Revises: 54d751c9fabe
Create Date: 2019-11-17 20:47:03.092732

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'fe8b8cd7c661'
down_revision = '54d751c9fabe'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('visualizations', sa.Column('version', sa.Integer(), nullable=True))
    op.execute('UPDATE visualizations SET version=1')
    op.alter_column('visualizations', 'version', nullable=False)
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('visualizations', 'version')
    # ### end Alembic commands ###
