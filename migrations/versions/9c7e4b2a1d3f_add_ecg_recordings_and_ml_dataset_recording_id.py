"""add ecg_recordings and ml_dataset recording_id

Revision ID: 9c7e4b2a1d3f
Revises: 2f49809ddb6a
Create Date: 2026-11-22 06:35:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9c7e4b2a1d3f'
down_revision = '2f49809ddb6a'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if 'ecg_recordings' not in existing_tables:
        op.create_table(
            'ecg_recordings',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('history_id', sa.Integer(), nullable=False),
            sa.Column('case_id', sa.String(length=36), nullable=True),
            sa.Column('patient_id', sa.Integer(), nullable=False),
            sa.Column('sample_rate_hz', sa.Integer(), nullable=False),
            sa.Column('n_samples', sa.Integer(), nullable=False),
            sa.Column('signal', sa.LargeBinary(), nullable=False),
            sa.Column('attention', sa.LargeBinary(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['history_id'], ['patient_history.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['patient_id'], ['users.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('history_id', name='uq_ecg_recordings_history_id'),
        )
        with op.batch_alter_table('ecg_recordings', schema=None) as batch_op:
            batch_op.create_index(batch_op.f('ix_ecg_recordings_case_id'), ['case_id'], unique=False)
            batch_op.create_index(batch_op.f('ix_ecg_recordings_created_at'), ['created_at'], unique=False)
            batch_op.create_index(batch_op.f('ix_ecg_recordings_patient_id'), ['patient_id'], unique=False)

    ml_cols = {c['name'] for c in inspector.get_columns('ml_dataset')}
    if 'recording_id' not in ml_cols:
        with op.batch_alter_table('ml_dataset', schema=None) as batch_op:
            batch_op.add_column(sa.Column('recording_id', sa.Integer(), nullable=True))
            batch_op.alter_column('signal', existing_type=sa.Text(), nullable=True)
            batch_op.create_index(batch_op.f('ix_ml_dataset_recording_id'), ['recording_id'], unique=False)
            batch_op.create_foreign_key(
                'fk_ml_dataset_recording_id_ecg_recordings',
                'ecg_recordings',
                ['recording_id'],
                ['id'],
                ondelete='SET NULL',
            )


def downgrade():
    with op.batch_alter_table('ml_dataset', schema=None) as batch_op:
        batch_op.drop_constraint('fk_ml_dataset_recording_id_ecg_recordings', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_ml_dataset_recording_id'))
        batch_op.alter_column('signal', existing_type=sa.Text(), nullable=False)
        batch_op.drop_column('recording_id')

    with op.batch_alter_table('ecg_recordings', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ecg_recordings_patient_id'))
        batch_op.drop_index(batch_op.f('ix_ecg_recordings_created_at'))
        batch_op.drop_index(batch_op.f('ix_ecg_recordings_case_id'))

    op.drop_table('ecg_recordings')
