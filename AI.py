from datetime import datetime
import os
import time

from flask import Flask, make_response, request, send_file
from gtts import gTTS
import requests
import speech_recognition as sr

app = Flask(__name__)
recognizer = sr.Recognizer()

# Biến toàn cục quản lý trạng thái thức/ngủ và timeout 20 giây
is_awake = False
last_active_time = 0
SLEEP_TIMEOUT = 60

print("[Server] Đã sẵn sàng chạy theo yêu cầu mới!")


@app.route("/")
def home():
    return "AI Speaker Server Running!"


@app.route("/process-audio", methods=["POST"])
def process_audio():
    global is_awake, last_active_time

    # 1. Xử lý sự kiện hệ thống (boot, connected)
    if request.is_json:
        data = request.get_json()
        event_type = data.get("type", "")
        if event_type == "boot" or event_type == "connected":
            is_awake = False
            reply_text = "Kết nối server thành công"
            print(f"[Server] Sự kiện hệ thống - Phản hồi: {reply_text}")

            mp3_path = "response.mp3"
            raw_pcm_reply = "response.pcm"
            tts = gTTS(text=reply_text, lang="vi")
            tts.save(mp3_path)
            os.system(
                f"ffmpeg -y -i {mp3_path} -f s16le -acodec pcm_s16le -ar 16000 -ac 1 {raw_pcm_reply} > /dev/null 2>&1"
            )

            if os.path.exists(raw_pcm_reply):
                resp = make_response(
                    send_file(raw_pcm_reply, mimetype="application/octet-stream")
                )
                resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
                return resp
        return "", 204

    # 2. Kiểm tra xem đã quá 20 giây kể từ LẦN PHẢN HỒI TRƯỚC ĐÓ hay chưa
    if is_awake and (time.time() - last_active_time > SLEEP_TIMEOUT):
        is_awake = False
        print("[Server] Đã quá 20 giây không có tương tác, tự động chuyển về chế độ NGỦ.")

    # 3. Nhận audio thô từ ESP32
    audio_data = request.data
    if len(audio_data) < 1000:
        resp = make_response("", 204)
        resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
        return resp

    raw_pcm_path = "input_temp.pcm"
    wav_path = "input_temp.wav"

    with open(raw_pcm_path, "wb") as f:
        f.write(audio_data)

    os.system(
        f"ffmpeg -y -f s16le -ar 16000 -ac 1 -i {raw_pcm_path} {wav_path} > /dev/null 2>&1"
    )

    spoken_text = ""
    try:
        with sr.AudioFile(wav_path) as source:
            audio = recognizer.record(source)
            spoken_text = recognizer.recognize_google(
                audio, language="vi-VN"
            ).lower()
            print(f"[Server] Nghe được: '{spoken_text}'")
    except sr.UnknownValueError:
        print("[Server] Không nghe rõ nội dung.")
        resp = make_response("", 204)
        resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
        return resp
    except sr.RequestError as e:
        print(f"[Server] Lỗi kết nối Google STT: {e}")
        return "", 500

    reply_text = ""

    # 4. Phân rã logic theo trạng thái THỨC hay NGỦ
    if not is_awake:
        # Đang ngủ: Chỉ bắt từ khóa đánh thức
        if "xin chào" in spoken_text or "chào" in spoken_text:
            is_awake = True
            reply_text = "Chào sếp, sếp cần giúp gì ạ?"
            print("[Server] Trạng thái: ĐÃ THỨC.")
        else:
            # Đang ngủ mà nói câu khác -> Lờ đi
            resp = make_response("", 204)
            resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
            return resp
    else:
        # Đang thức: Xử lý các câu lệnh chức năng
        if "nhiệt độ" in spoken_text or "nhiệt" in spoken_text:
            reply_text = "nhiệt độ 34 độ C"
        elif "mấy giờ rồi" in spoken_text or "giờ" in spoken_text:
            now = datetime.now()
            reply_text = f"Bây giờ là {now.strftime('%H')} giờ {now.strftime('%M')} phút"
    elif "thời tiết" in spoken_text or "thời tiết" in spoken_text:
            try:
                # Tách lấy phần sau chữ "thời tiết" để làm tên tỉnh/thành phố
                parts = spoken_text.split("thời tiết")
                location = "HaNam"  # Mặc định nếu chỉ nói mỗi chữ "thời tiết"
                if len(parts) > 1 and parts[1].strip() != "":
                    # Lấy từ tiếp theo sau chữ thời tiết và chuẩn hóa lại
                    raw_loc = parts[1].strip()
                    # Loại bỏ các từ thừa nếu có, viết hoa chữ cái đầu hoặc giữ nguyên cho wttr.in xử lý
                    location = raw_loc.replace(" ", "")  # wttr.in hỗ trợ viết liền không dấu hoặc có dấu

                response = requests.get(
                    f"https://wttr.in/{location}?format=j1", timeout=5
                )
                if response.status_code == 200:
                    data = response.json()
                    temp = data["current_condition"][0]["temp_C"]
                    humidity = data["current_condition"][0]["humidity"]
                    reply_text = f"Nhiệt độ tại {location} là {temp} độ C và độ ẩm là {humidity} phần trăm"
                else:
                    reply_text = f"Không tìm thấy dữ liệu thời tiết cho {location}"
            except Exception as e:
                print(f"[Lỗi thời tiết chi tiết]: {e}")
                reply_text = "Lỗi kết nối thời tiết"
        elif "ngủ đi" in spoken_text or "tắt đi" in spoken_text:
            is_awake = False
            reply_text = "Tôi đi ngủ đây."
            print("[Server] Trạng thái: Đã chuyển về NGỦ theo yêu cầu.")
        else:
            reply_text = "Tôi không hiểu yêu cầu này."

    print(f"[Server] Phản hồi: {reply_text}")

    # 5. Tạo file âm thanh trả về
    mp3_path = "response.mp3"
    raw_pcm_reply = "response.pcm"

    tts = gTTS(text=reply_text, lang="vi")
    tts.save(mp3_path)
    os.system(
        f"ffmpeg -y -i {mp3_path} -f s16le -acodec pcm_s16le -ar 16000 -ac 1 {raw_pcm_reply} > /dev/null 2>&1"
    )

    # Cập nhật mốc thời gian 20s BẮT ĐẦU TỪ KHI SERVER TẠO XONG PHẢN HỒI (sau khi bot nói xong)
    if is_awake:
        last_active_time = time.time()

    if os.path.exists(raw_pcm_reply):
        resp = make_response(
            send_file(raw_pcm_reply, mimetype="application/octet-stream")
        )
        resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
        return resp
    else:
        return "", 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
