# Mini Agar.io Clone (Python + FastAPI + WebSocket)

Chạy server:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python server/main.py
```

Mở trình duyệt tại: http://localhost:8000

Điều khiển: Di chuyển chuột để chỉ hướng / vị trí mục tiêu. Bóng sẽ dần di chuyển đến đó, ăn thức ăn để lớn hơn. Đụng nhau: bóng lớn hơn (lớn hơn 10%) sẽ ăn bóng nhỏ và tích lũy kích thước.

Ý tưởng mở rộng:
- Thêm chia tách (split) / phóng tia (shoot mass)
- Hiệu ứng zoom theo kích thước
- Giới hạn camera mềm
- Âm thanh khi ăn / khi chết
- Tối ưu hoá cập nhật (chỉ gửi delta)
- Thêm xác thực tên người chơi
