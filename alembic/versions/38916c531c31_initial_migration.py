"""Initial migration

Revision ID: 38916c531c31
Revises: 
Create Date: 2024-03-03 20:45:59.068412

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '38916c531c31'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('cells',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('cell_name', sa.String(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('cell_name')
    )
    op.create_table('testrecords',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('test_name', sa.String(), nullable=True),
    sa.Column('test_type', sa.String(), nullable=True),
    sa.Column('cell_name', sa.String(), nullable=True),
    sa.Column('test_data', sa.LargeBinary(), nullable=True),
    sa.Column('test_metadata', sa.LargeBinary(), nullable=True),
    sa.ForeignKeyConstraint(['cell_name'], ['cells.cell_name'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('test_name')
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table('testrecords')
    op.drop_table('cells')
    # ### end Alembic commands ###
