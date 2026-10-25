"""initial migration

Revision ID: 001_initial
Revises: 
Create Date: 2026-08-17 02:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '001_initial'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. users
    op.create_table('users',
    sa.Column('telegram_id', sa.BigInteger(), nullable=False),
    sa.Column('username', sa.String(), nullable=True),
    sa.Column('reputation_score', sa.Numeric(), server_default='0', nullable=False),
    sa.Column('completed_deals_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('is_banned', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('telegram_id')
    )
    
    # 2. offers
    op.create_table('offers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=True),
    sa.Column('offer_type', sa.String(), nullable=True),
    sa.Column('rate', sa.Numeric(), nullable=False),
    sa.Column('min_limit', sa.Numeric(), nullable=False),
    sa.Column('max_limit', sa.Numeric(), nullable=False),
    sa.Column('payment_methods', postgresql.ARRAY(sa.String()), nullable=True),
    sa.Column('status', sa.String(), server_default='active', nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("offer_type IN ('sell', 'buy')", name='chk_offer_type'),
    sa.ForeignKeyConstraint(['user_id'], ['users.telegram_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    
    # 3. deals
    op.create_table('deals',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('offer_id', sa.Integer(), nullable=True),
    sa.Column('seller_id', sa.BigInteger(), nullable=True),
    sa.Column('buyer_id', sa.BigInteger(), nullable=True),
    sa.Column('amount_usdt', sa.Numeric(), nullable=False),
    sa.Column('amount_syp', sa.Numeric(), nullable=False),
    sa.Column('fee_usdt', sa.Numeric(), server_default='1.0', nullable=False),
    sa.Column('deposit_address', sa.String(), nullable=False),
    sa.Column('deposit_index', sa.Integer(), nullable=False),
    sa.Column('buyer_payout_address', sa.String(), nullable=False),
    sa.Column('seller_payment_info', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.String(), server_default='CREATED', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['buyer_id'], ['users.telegram_id'], ),
    sa.ForeignKeyConstraint(['offer_id'], ['offers.id'], ),
    sa.ForeignKeyConstraint(['seller_id'], ['users.telegram_id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('deposit_index')
    )
    op.create_index(op.f('ix_deals_deposit_address'), 'deals', ['deposit_address'], unique=True)
    op.create_index(op.f('ix_deals_status'), 'deals', ['status'], unique=False)
    
    # 4. payment_proofs
    op.create_table('payment_proofs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('deal_id', sa.Integer(), nullable=True),
    sa.Column('transaction_reference', sa.String(), nullable=False),
    sa.Column('amount', sa.Numeric(), nullable=False),
    sa.Column('submitted_by', sa.BigInteger(), nullable=True),
    sa.Column('submitted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['deal_id'], ['deals.id'], ),
    sa.ForeignKeyConstraint(['submitted_by'], ['users.telegram_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_payment_proofs_transaction_reference'), 'payment_proofs', ['transaction_reference'], unique=True)
    
    # 5. disputes
    op.create_table('disputes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('deal_id', sa.Integer(), nullable=True),
    sa.Column('opened_by', sa.BigInteger(), nullable=True),
    sa.Column('reason', sa.String(), nullable=True),
    sa.Column('resolution', sa.String(), nullable=True),
    sa.Column('resolved_by', sa.BigInteger(), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['deal_id'], ['deals.id'], ),
    sa.ForeignKeyConstraint(['opened_by'], ['users.telegram_id'], ),
    sa.ForeignKeyConstraint(['resolved_by'], ['users.telegram_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    
    # 6. blockchain_transactions
    op.create_table('blockchain_transactions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('deal_id', sa.Integer(), nullable=True),
    sa.Column('tx_hash', sa.String(), nullable=False),
    sa.Column('direction', sa.String(), nullable=True),
    sa.Column('amount', sa.Numeric(), nullable=False),
    sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("direction IN ('deposit', 'release')", name='chk_direction'),
    sa.ForeignKeyConstraint(['deal_id'], ['deals.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tx_hash')
    )


def downgrade() -> None:
    op.drop_table('blockchain_transactions')
    op.drop_table('disputes')
    op.drop_index(op.f('ix_payment_proofs_transaction_reference'), table_name='payment_proofs')
    op.drop_table('payment_proofs')
    op.drop_index(op.f('ix_deals_status'), table_name='deals')
    op.drop_index(op.f('ix_deals_deposit_address'), table_name='deals')
    op.drop_table('deals')
    op.drop_table('offers')
    op.drop_table('users')
