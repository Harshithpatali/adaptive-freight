from __future__ import annotations
import json
from sqlalchemy import create_engine,MetaData,Table,Column,Integer,String,Text,DateTime,func
from .config import settings
_connect_args={"check_same_thread":False} if settings.database_url.startswith("sqlite") else {}
engine=create_engine(settings.database_url,connect_args=_connect_args,future=True)
metadata=MetaData()
checkpoints=Table("checkpoints",metadata,Column("id",Integer,primary_key=True,autoincrement=True),Column("created_at",DateTime(timezone=True),server_default=func.now()),Column("payload",Text,nullable=False))
orders_audit=Table("orders_audit",metadata,Column("shipment_id",String,primary_key=True),Column("received_at",DateTime(timezone=True),server_default=func.now()),Column("payload",Text,nullable=False))
def init_db(): metadata.create_all(engine)
def save_checkpoint(state):
    with engine.begin() as conn:
        conn.execute(checkpoints.insert().values(payload=json.dumps(state,default=str)))
        ids=[r[0] for r in conn.execute(checkpoints.select().with_only_columns(checkpoints.c.id).order_by(checkpoints.c.id.desc())).fetchall()]
        for old_id in ids[5:]: conn.execute(checkpoints.delete().where(checkpoints.c.id==old_id))
def load_latest_checkpoint():
    with engine.begin() as conn:
        row=conn.execute(checkpoints.select().order_by(checkpoints.c.id.desc()).limit(1)).fetchone()
        return json.loads(row.payload) if row else None
def record_order_if_new(shipment_id,payload):
    with engine.begin() as conn:
        if conn.execute(orders_audit.select().where(orders_audit.c.shipment_id==shipment_id)).fetchone(): return False
        conn.execute(orders_audit.insert().values(shipment_id=shipment_id,payload=json.dumps(payload,default=str))); return True
