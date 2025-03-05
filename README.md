#  Saga Pattern for E-commerce Checkout (Single Microservice)

## Goal
Implementation of the **Saga Pattern** within a single microservice for the orchestration of the "checkout" business process with steps:
- **Payment**
- **Inventory (reserve of goods)**
- **Shipping**

Each step supports:
- **`do` (execution)**
- **`compensate' (rollback)**

If an error occurs at any stage, the previously successful steps are compensated in reverse order.

---

##  Architecture of solution


![Диаграмма](https://github.com/assanix/saga-fastapi/blob/main/work-flow-saga.png) 

### Main components:

#### FastAPI (Saga Manager)
- REST API for start saga (`POST /checkout`) and check status (`GET /saga/{order_id}`).
- Create object `OrderSaga` and save in **Redis**.
- Initialize chain using **RabbitMQ**.
- In case of failures, initiates compensation by publishing the event in `compensation_queue`.


#### RabbitMQ (Event Broker)
- Connects all steps via events:
  - `payment.start`
  - `inventory.reserve`
  - `shipping.create`
  - `compensation.start`
- Each event gets into its own queue and is processed in the appropriate step.

#### Redis (State Store)
- Stores the current status of each saga.
- Allows you to retrieve and update the status of the saga at each stage.
- Used for crash recovery.


#### OrderSaga (Saga Context)
Model with execution status:
```python
order_id: str
status: SagaStatus
payment_status: SagaStatus
inventory_status: SagaStatus
shipping_status: SagaStatus
```
Statuses:
- **PENDING**
- **SUCCEEDED**
- **FAILED**
- **COMPENSATING**
- **COMPENSATED**


## How the System Works

### Successful Execution Flow:
1. The user sends a request to `/checkout` — a new saga is created, saved in Redis, and a `PAYMENT_INITIATED` event is published.

2. **Payment step**:
   - Listens for the event from the `payment_queue`.
   - Executes the "do" logic.
   - If successful → publishes the `INVENTORY_RESERVE` event.

3. **Inventory step**:
   - Listens for the event from the `inventory_queue`.
   - Executes the "do" logic.
   - If successful → publishes the `SHIPPING_CREATE` event.

4. **Shipping step**:
   - Listens for the event from the `shipping_queue`.
   - Executes the "do" logic.
   - If successful → updates the saga status in Redis to `SUCCEEDED`.


### Error and Compensation Flow:
- If any step raises an exception, the `trigger_compensation()` method is called.
- A `COMPENSATION_START` event is published to the `compensation_queue`.
- In `process_compensation_event()`:
  - Previously successful steps are compensated in reverse order:
    - First `Shipping` (if it was completed).
    - Then `Inventory`.
    - Then `Payment`.
  - The final saga status is updated in Redis as `COMPENSATED`.


## What Makes This Design Unique:
- A single microservice manages the entire orchestration.
- RabbitMQ and Redis are used for asynchronous processing and state persistence.
- Both "do" and "compensate" actions are fully implemented.
- No external microservices are required — all logic is contained within a single application.


## Running the Application
```bash
# Make sure RabbitMQ and Redis are running.
docker-compose up -d

# Run the application
uvicorn main:app --host 0.0.0.0 --port 8000
```


## Endpoints:
- `POST /checkout` — starts a new saga.
- `GET /saga/{order_id}` — retrieves the status of a saga.


## Technologies:
- FastAPI
- RabbitMQ (aio_pika)
- Redis (aioredis)
- Pydantic
- Uvicorn
