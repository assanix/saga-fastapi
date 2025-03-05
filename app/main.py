import asyncio
import uuid
import json
from enum import Enum
from typing import Dict, Optional

import aio_pika
import aioredis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import uvicorn

class SagaStatus(str, Enum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    COMPENSATING = "COMPENSATING"
    COMPENSATED = "COMPENSATED"

class SagaEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    order_id: str
    type: str
    payload: Dict
    status: SagaStatus = SagaStatus.PENDING

class OrderSaga(BaseModel):
    order_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    status: SagaStatus = SagaStatus.PENDING
    payment_status: SagaStatus = SagaStatus.PENDING
    inventory_status: SagaStatus = SagaStatus.PENDING
    shipping_status: SagaStatus = SagaStatus.PENDING

class RabbitMQHandler:
    def __init__(self, rabbitmq_url="amqp://admin:admin123@rabbitmq:5672/", redis_url="redis://localhost"):
        self.connection = None
        self.channel = None
        self.rabbitmq_url = rabbitmq_url
        self.redis_url = redis_url
        self.redis = None

    async def connect(self):
        self.connection = await aio_pika.connect_robust(self.rabbitmq_url)
        self.channel = await self.connection.channel()
        self.redis = aioredis.from_url(self.redis_url, decode_responses=True)

        exchange = await self.channel.declare_exchange('saga_events', aio_pika.ExchangeType.TOPIC)
        await (await self.channel.declare_queue('payment_queue', durable=True)).bind(exchange, 'payment.start')
        await (await self.channel.declare_queue('inventory_queue', durable=True)).bind(exchange, 'inventory.reserve')
        await (await self.channel.declare_queue('shipping_queue', durable=True)).bind(exchange, 'shipping.create')
        await (await self.channel.declare_queue('compensation_queue', durable=True)).bind(exchange, 'compensation.start')

    async def save_saga(self, saga: OrderSaga):
        await self.redis.set(saga.order_id, saga.model_dump_json())

    async def get_saga(self, order_id: str) -> Optional[OrderSaga]:
        data = await self.redis.get(order_id)
        if data:
            return OrderSaga.parse_raw(data)
        return None

    async def publish_event(self, routing_key: str, event: SagaEvent):
        exchange = await self.channel.get_exchange('saga_events')
        await exchange.publish(
            aio_pika.Message(body=event.model_dump_json().encode()),
            routing_key=routing_key
        )
    
    async def trigger_compensation(self, order_id: str):
        compensation_event = SagaEvent(
            order_id=order_id,
            type='COMPENSATION_START',
            payload={}
        )
        await self.publish_event('compensation.start', compensation_event)

    async def start_saga(self, payment_amount: float, product_id: str, quantity: int, shipping_address: str):
        saga = OrderSaga()
        await self.save_saga(saga)

        payment_event = SagaEvent(
            order_id=saga.order_id, 
            type='PAYMENT_INITIATED',
            payload={
                'amount': payment_amount,
                'product_id': product_id,
                'quantity': quantity,
                'shipping_address': shipping_address
            }
        )
        await self.publish_event('payment.start', payment_event)
        return saga.order_id

    async def setup_consumers(self):
        await (await self.channel.declare_queue('payment_queue')).consume(self.process_payment_event)
        await (await self.channel.declare_queue('inventory_queue')).consume(self.process_inventory_event)
        await (await self.channel.declare_queue('shipping_queue')).consume(self.process_shipping_event)
        await (await self.channel.declare_queue('compensation_queue')).consume(self.process_compensation_event)

    async def process_payment_event(self, message: aio_pika.IncomingMessage):
        async with message.process():
            try:
                event_data = json.loads(message.body.decode())
                saga = await self.get_saga(event_data['order_id'])
                if not saga:
                    return

                saga.payment_status = SagaStatus.SUCCEEDED
                await self.save_saga(saga)

                inventory_event = SagaEvent(
                    order_id=saga.order_id,
                    type='INVENTORY_RESERVE',
                    payload={
                        'product_id': event_data['payload']['product_id'],
                        'quantity': event_data['payload']['quantity'],
                        'shipping_address': event_data['payload']['shipping_address']
                    }
                )
                await self.publish_event('inventory.reserve', inventory_event)

            except Exception:
                await self.trigger_compensation(event_data['order_id'])

    async def process_inventory_event(self, message: aio_pika.IncomingMessage):
        async with message.process():
            try:
                event_data = json.loads(message.body.decode())
                saga = await self.get_saga(event_data['order_id'])
                if not saga:
                    return

                saga.inventory_status = SagaStatus.SUCCEEDED
                await self.save_saga(saga)

                shipping_event = SagaEvent(
                    order_id=saga.order_id,
                    type='SHIPPING_CREATE',
                    payload={
                        'shipping_address': event_data['payload']['shipping_address']
                    }
                )
                await self.publish_event('shipping.create', shipping_event)

            except Exception:
                await self.trigger_compensation(event_data['order_id'])

    async def process_shipping_event(self, message: aio_pika.IncomingMessage):
        async with message.process():
            try: 
                event_data = json.loads(message.body.decode())
                saga = await self.get_saga(event_data['order_id'])
                if not saga:
                    return

                saga.shipping_status = SagaStatus.SUCCEEDED
                saga.status = SagaStatus.SUCCEEDED
                await self.save_saga(saga)

            except Exception:
                await self.trigger_compensation(event_data['order_id'])

    async def process_compensation_event(self, message: aio_pika.IncomingMessage):
        async with message.process():
            event_data = json.loads(message.body.decode())
            saga = await self.get_saga(event_data['order_id'])
            if not saga:
                return

            saga.status = SagaStatus.COMPENSATING

            if saga.shipping_status == SagaStatus.SUCCEEDED:
                saga.shipping_status = SagaStatus.COMPENSATED
            if saga.inventory_status == SagaStatus.SUCCEEDED:
                saga.inventory_status = SagaStatus.COMPENSATED
            if saga.payment_status == SagaStatus.SUCCEEDED:
                saga.payment_status = SagaStatus.COMPENSATED

            saga.status = SagaStatus.COMPENSATED
            await self.save_saga(saga)

app = FastAPI()
rabbit_handler = RabbitMQHandler()

@app.on_event("startup")
async def startup_event():
    await rabbit_handler.connect()
    await rabbit_handler.setup_consumers()

@app.post("/checkout")
async def create_checkout(payment_amount: float, product_id: str, quantity: int, shipping_address: str):
    order_id = await rabbit_handler.start_saga(payment_amount, product_id, quantity, shipping_address)
    return {"order_id": order_id}

@app.get("/saga/{order_id}")
async def get_saga_status(order_id: str):
    saga = await rabbit_handler.get_saga(order_id)
    if not saga:
        raise HTTPException(status_code=404, detail="Saga not found")
    return saga

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
