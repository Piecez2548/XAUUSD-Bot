# รายงานการพัฒนา XAUUSD AI Trader — Phase 1

วันที่จัดทำ: 21 กันยายน 2026  
สถานะ: **เสร็จสมบูรณ์และผ่านการตรวจสอบ**  
ขอบเขต: โครงสร้างพื้นฐาน MetaTrader 5 แบบอ่านอย่างเดียว (Read-only)

## 1. สรุปผล

ได้พัฒนาโครงสร้างพื้นฐานสำหรับเชื่อมต่อ MetaTrader 5 และอ่านข้อมูล XAUUSD ตามขอบเขต
Phase 1 ครบถ้วนแล้ว ระบบสามารถเชื่อมต่อเทอร์มินัล ตรวจสอบบัญชี ค้นหาสัญลักษณ์ทองคำ
อ่านข้อกำหนดของสัญลักษณ์ อ่าน Tick และ OHLCV หลาย Timeframe อ่านสถานะ Position
ตรวจสอบความถูกต้องของข้อมูล และสร้าง `MarketSnapshot` ที่มีชนิดข้อมูลชัดเจน

ไม่มีการพัฒนา Phase 2 และไม่มีเส้นทางการทำงานที่สามารถเปิด แก้ไข หรือปิดคำสั่งซื้อขายได้

ผลการทดสอบกับ MT5 Desktop ที่ล็อกอินอยู่จริง:

- เชื่อมต่อเทอร์มินัลสำเร็จ
- ตรวจพบบัญชี Demo สำเร็จ
- เลือกสัญลักษณ์ `XAUUSD` จากผู้สมัคร 2 รายการสำเร็จ
- อ่านและตรวจสอบ M5 จำนวน 1,000 แท่งสำเร็จ
- อ่านและตรวจสอบ M15 จำนวน 1,000 แท่งสำเร็จ
- อ่านและตรวจสอบ H1 จำนวน 500 แท่งสำเร็จ
- อ่านและตรวจสอบ H4 จำนวน 500 แท่งสำเร็จ
- อ่าน Position ที่เปิดอยู่สำเร็จ
- สร้าง `MarketSnapshot` สำเร็จ
- ปิดการเชื่อมต่อ MT5 อย่างถูกต้อง
- กระบวนการจบด้วย Exit Code `0` และสถานะ `PASS`

## 2. สถาปัตยกรรม

```text
xauusd-ai-trader/
├── main.py
├── README.md
├── requirements.txt
├── requirements-dev.txt
├── pyproject.toml
├── .env.example
├── .gitignore
├── config/
│   ├── __init__.py
│   └── settings.py
├── models/
│   ├── __init__.py
│   └── market.py
├── mt5/
│   ├── __init__.py
│   ├── connection.py
│   ├── account.py
│   ├── symbols.py
│   ├── market_data.py
│   └── positions.py
├── utils/
│   ├── __init__.py
│   └── logging.py
├── tests/
│   ├── conftest.py
│   ├── test_connection_and_safety.py
│   ├── test_market_data.py
│   ├── test_models.py
│   ├── test_readers.py
│   └── test_symbols.py
└── logs/
    └── .gitkeep
```

### หน้าที่ของแต่ละส่วน

- `main.py` ควบคุมลำดับการทำงานและแสดงรายงาน Diagnostic สำหรับผู้ใช้
- `config/settings.py` อ่านและตรวจสอบค่า Environment โดยไม่ฝัง Credential ในโค้ด
- `mt5/connection.py` จัดการวงจรชีวิตการเชื่อมต่อ MT5 และการ Shutdown
- `mt5/account.py` อ่านสถานะบัญชีแบบ Read-only
- `mt5/symbols.py` ค้นหาและเลือกสัญลักษณ์ทองคำ อ่าน Specification และ Tick
- `mt5/market_data.py` อ่านและตรวจสอบข้อมูล OHLCV
- `mt5/positions.py` อ่าน Position ที่เปิดอยู่ของสัญลักษณ์ที่เลือก
- `models/market.py` กำหนด Pydantic Model แบบ Immutable
- `utils/logging.py` บันทึก Structured Log แบบ JSONL พร้อมปกปิด Secret
- `tests/` ทดสอบ Logic โดยไม่ต้องใช้ MT5 Terminal จริง

MT5 ถูกแยกเป็น Integration Boundary โดยโมดูลในอนาคตควรรับข้อมูลผ่าน
`MarketSnapshot` และไม่ควรได้รับ MT5 API โดยตรง

## 3. ความสามารถที่พัฒนา

### 3.1 การเชื่อมต่อ MT5

- เริ่มต้น MetaTrader5 ผ่าน Python Integration
- ใช้บัญชีที่ล็อกอินใน MT5 Desktop เป็นค่าเริ่มต้น
- รองรับ Terminal Path และ Credential ผ่าน Environment Variable เมื่อจำเป็น
- ตรวจสอบว่าเทอร์มินัลเชื่อมต่อ Broker อยู่จริง
- ตรวจสอบว่าอ่าน Account Information ได้
- แสดงข้อผิดพลาดจาก MT5 อย่างชัดเจน
- เรียก Shutdown เมื่อจบการทำงานและเมื่อ Initialization/Verification ล้มเหลว

### 3.2 การค้นหาสัญลักษณ์ทองคำ

- ไม่สมมติว่าสัญลักษณ์ต้องชื่อ `XAUUSD` เท่านั้น
- รองรับชื่อ เช่น `XAUUSD.a`, `XAUUSDm`, `GOLD` และชื่อที่มี Metadata ของ XAU/USD
- จัดอันดับ Candidate ด้วยกฎที่ Deterministic
- เลือกอัตโนมัติเฉพาะเมื่อมี Candidate ที่ดีที่สุดเพียงรายการเดียว
- หากกำกวม ระบบจะหยุด แสดงรายชื่อ Candidate และขอให้ตั้ง `TRADING_SYMBOL`
- Override ต้องตรงกับชื่อ Symbol ของ Broker แบบ Exact Match โดยไม่สนตัวพิมพ์เล็ก/ใหญ่

### 3.3 Symbol Specification และ Tick

ข้อมูลที่ระบบอ่านและเก็บ:

- ชื่อสัญลักษณ์
- Bid และ Ask
- Spread
- Digits และ Point
- Trade Tick Size
- Trade Tick Value
- Trade Tick Value Profit/Loss
- Contract Size
- Volume Minimum/Maximum/Step
- Trade Mode
- Tick Timestamp แบบ UTC และ Raw Epoch Timestamp

### 3.4 Account State

ข้อมูลที่ระบบอ่าน:

- Balance
- Equity
- Margin
- Free Margin
- Margin Level
- Profit
- Leverage
- Currency
- Server
- Account Trade Mode

ระบบไม่แสดง Password หรือข้อมูล Authentication Secret ใน Output และ Log

### 3.5 Market Data

ค่าเริ่มต้นของข้อมูลย้อนหลัง:

| Timeframe | จำนวนแท่ง |
|---|---:|
| M5 | 1,000 |
| M15 | 1,000 |
| H1 | 500 |
| H4 | 500 |

ข้อมูลต่อแท่งประกอบด้วย Timestamp, Open, High, Low, Close, Tick Volume, Spread และ
Real Volume

Timestamp ถูกแปลงเป็น `datetime` ที่มี UTC Timezone อย่างชัดเจน และยังเก็บ Raw Epoch
Timestamp ไว้ ไม่มีการสร้าง เติม หรือ Forward-fill แท่งราคาที่ขาดหาย

### 3.6 การตรวจสอบข้อมูล

ระบบจะ Fail Closed หากพบว่า:

- DataFrame ว่าง
- Column ที่จำเป็นหายไป
- จำนวนแท่งต่ำกว่าเกณฑ์ที่กำหนด
- Timestamp ไม่เรียงจากเก่าไปใหม่
- Timestamp ซ้ำ
- OHLC เป็น Null หรือไม่ใช่ตัวเลข
- ราคามีค่าไม่เป็นบวก
- High ต่ำกว่า Open หรือ Close
- Low สูงกว่า Open หรือ Close
- High ต่ำกว่า Low
- Volume หรือ Spread ติดลบหรือไม่ใช่ตัวเลข

### 3.7 Position

ข้อมูล Position ที่อ่าน:

- Ticket
- Symbol
- Type และชื่อ BUY/SELL
- Volume
- Open Price และ Current Price
- Stop Loss และ Take Profit
- Profit และ Swap
- Magic Number
- Comment
- Open Time แบบ UTC และ Raw Epoch Timestamp

ส่วนนี้เป็น Read-only และไม่มี API สำหรับแก้ไข Position

### 3.8 MarketSnapshot

`MarketSnapshot` ประกอบด้วย:

- Account State
- Symbol Specification
- Current Tick
- Open Positions
- Candles สำหรับ M5, M15, H1 และ H4
- Generated Timestamp แบบ UTC

Model เป็น Immutable, ห้ามมี Field ที่ไม่รู้จัก และปฏิเสธ NaN/Infinity รวมถึงตรวจสอบว่า
ทุก Timeframe มีข้อมูล และ Position ทั้งหมดตรงกับ Symbol ที่เลือก

## 4. Dependencies

### Runtime

- `MetaTrader5>=5.0.45,<6`
- `pandas>=2.2,<3`
- `pydantic>=2.8,<3`
- `python-dotenv>=1.0,<2`

### Development

- `pytest>=8.2,<9`
- `ruff>=0.6,<1`

## 5. ผลการตรวจสอบ

| การตรวจสอบ | ผลลัพธ์ |
|---|---|
| Python Unit Tests | ผ่าน 27/27 Tests |
| Ruff Lint | ผ่าน ไม่มีข้อผิดพลาด |
| Python Compile/Import Validation | ผ่าน |
| Live MT5 Read-only Smoke Test | ผ่าน |
| Root TypeScript Build (`tsc -b`) | ผ่าน |
| Root Vite Production Build | ผ่าน |
| Root ESLint/Oxlint | ผ่าน |

Unit Test ครอบคลุม:

- Connection Lifecycle และ Clean Shutdown
- Initialization/Connection Failure
- การส่ง Credential โดยไม่แสดงใน Representation
- การปกปิด Secret ใน Message และ Exception Traceback
- การตรวจสอบ OHLC
- Empty DataFrame
- Timestamp ซ้ำและไม่เรียงลำดับ
- ราคาที่ไม่ถูกต้อง
- จำนวนแท่งต่ำกว่าเกณฑ์
- การแปลง Timestamp เป็น UTC
- Symbol Ranking, Exact Override และ Ambiguity
- Account/Symbol/Tick/Position Mapping
- Snapshot Validation และ Immutability
- Source-level Safety Guard สำหรับคำสั่งซื้อขายต้องห้าม

## 6. Security และ Safety

- ไม่มีการเรียก `order_send`
- ไม่มี Market Order หรือ Pending Order
- ไม่มีการแก้ไขหรือปิด Position
- ไม่มีการแก้ไข Stop Loss หรือ Take Profit
- ไม่มี Execution Engine
- ไม่มี Autonomous Trading Logic
- ไม่มี AI Decision Logic
- ไม่มี Credential ฝังใน Source Code
- `.env`, Virtual Environment, Cache และ Runtime Log ถูก Ignore จาก Git
- Password ไม่ปรากฏใน Dataclass Representation
- Structured Logger ปกปิด Secret ทั้งในข้อความและ Exception Traceback
- Symbol ที่กำกวมทำให้ระบบหยุดแทนการคาดเดา
- ข้อมูลที่ไม่ถูกต้องทำให้ระบบหยุดแทนการส่งต่อไปยัง Component อื่น

## 7. สิ่งที่ยังไม่ได้ทดสอบกับระบบจริง

- การ Login ด้วย Credential ที่ระบุเอง เนื่องจากการทดสอบใช้บัญชีที่ล็อกอินอยู่ใน MT5
- การอ่าน Position จริงที่มีจำนวนมากกว่าศูนย์ เนื่องจากบัญชีทดสอบไม่มี Position เปิดอยู่
- Broker รายอื่นที่ใช้ Suffix หรือชื่อทองคำแตกต่างจากเทอร์มินัลที่ทดสอบ
- พฤติกรรมเมื่อ Broker มีข้อมูลย้อนหลังไม่ถึง Minimum Candle Ratio

เส้นทางเหล่านี้ได้รับการออกแบบให้ Fail Closed และ Logic หลักมี Unit Test แบบ Mock รองรับ

## 8. วิธีติดตั้งและใช้งาน

```powershell
cd "D:\Project_001\Nexus-Project\XAUUSD Bot"
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
python main.py
```

หากระบบรายงานว่า Symbol กำกวม ให้แก้ไข `.env` และกำหนดชื่อที่ตรงกับ Broker เช่น:

```dotenv
TRADING_SYMBOL=XAUUSD.a
```

คำสั่งตรวจสอบ:

```powershell
python -m pytest
python -m ruff check .
python -m compileall -q config mt5 models utils main.py
```

## 9. Output ที่คาดหวัง

```text
========================================
XAUUSD AI TRADER - PHASE 1
========================================

MT5
Status: CONNECTED

ACCOUNT
Balance: ...
Equity: ...
Currency: ...

SYMBOL
Broker symbol: ...
Bid: ...
Ask: ...
Spread: ... points

DATA
M5  : 1000 candles
M15 : 1000 candles
H1  : 500 candles
H4  : 500 candles

POSITIONS
Open positions: ...

PHASE 1 STATUS: PASS

READ-ONLY MODE
ORDER EXECUTION DISABLED
========================================
```

## 10. ปัญหาที่ต้องการข้อมูลจากผู้ใช้

ปัจจุบันไม่มีปัญหาที่ขัดขวางการใช้งาน Phase 1

เมื่อนำไปใช้กับ Broker รายอื่น ผู้ใช้อาจต้องระบุ `TRADING_SYMBOL` หาก Broker มีสัญลักษณ์
ทองคำหลายรายการที่มีคะแนนเท่ากัน ระบบจะไม่เลือกเองเพื่อป้องกันการใช้สัญลักษณ์ผิด

## 11. ข้อสรุป

Phase 1 พร้อมใช้งานเป็นฐานข้อมูล Read-only สำหรับระบบในอนาคต โดยรักษาหลัก Correctness,
Fail Closed, Typed Interface, Credential Safety และ MT5 Isolation ตามข้อกำหนด ระบบยังไม่มี
ความสามารถซื้อขายอัตโนมัติ และหยุดการพัฒนาไว้ที่ขอบเขต Phase 1 ตามคำสั่ง
