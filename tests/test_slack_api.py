import os
import logging
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from dotenv import load_dotenv
load_dotenv()

# 디버그용 로그 활성화
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", os.getenv("SLACK_BOT_TOKEN"))
TARGET_CHANNEL = "#general" 

client = WebClient(token=SLACK_BOT_TOKEN)


def test_auth_connection():
    """1. 인증 및 연결 테스트 (auth.test)"""
    print("\n=== 1. 인증 테스트 시작 ===")
    try:
        response = client.auth_test()
        print("✅ 연결 성공!")
        print(f" - 봇 이름: {response['user']}")
        print(f" - 워크스페이스: {response['team']}")
        # 다음 테스트를 위해 현재 인증된 봇의 유저 ID를 반환합니다.
        print(response)
        return response['user_id']
    except SlackApiError as e:
        print(f"❌ 인증 실패: {e.response['error']}")
        return None


def test_get_user_profile(user_id):
    """2. 유저 ID로 프로필 정보 조회 (users.info) 🌟 [추가됨]"""
    print(f"\n=== 2. 유저 프로필 조회 시작 (대상 ID: {user_id}) ===")
    try:
        # users.info API 호출
        response = client.users_info(user=user_id)
        user_info = response["user"]
        profile = user_info["profile"]
        
        print("✅ 유저 정보 조회 성공!")
        print(f" - 실제 이름(Real Name): {user_info.get('real_name')}")
        print(f" - 이메일(Email): {profile.get('email', '이메일 권한 없음')}")
    except SlackApiError as e:
        print(f"❌ 유저 정보 조회 실패: {e.response['error']}")
        print("💡 팁: 'user_not_found' 에러가 나면 올바른 유저 ID(예: U0123456789)인지 확인하세요.")


def test_list_channels():
    """3. 공개 채널 목록 조회 (conversations.list)"""
    print("\n=== 3. 채널 목록 조회 시작 ===")
    try:
        response = client.conversations_list(types="public_channel")
        channels = response["channels"]
        print(f"가져온 채널 개수: {len(channels)}개")
        for channel in channels[:5]:
            print(f" - 채널명: #{channel['name']} (ID: {channel['id']})")
    except SlackApiError as e:
        print(f"❌ 채널 목록 조회 실패: {e.response['error']}")


def test_send_message(channel, message):
    """4. 메시지 전송 테스트 (chat.postMessage)"""
    print("\n=== 4. 메시지 전송 시작 ===")
    try:
        response = client.chat_postMessage(channel=channel, text=message)
        print(f"✅ 메시지 전송 성공! (TS: {response['ts']})")
        return response['ts']
    except SlackApiError as e:
        print(f"❌ 메시지 전송 실패: {e.response['error']}")
        return None


if __name__ == "__main__":
    if "YOUR_SLACK_BOT_TOKEN_HERE" in SLACK_BOT_TOKEN:
        print("❌ 에러: 코드 상단의 'YOUR_SLACK_BOT_TOKEN_HERE'를 실제 Slack 봇 토큰으로 교체해 주세요.")
    else:
        # 1. 인증 테스트를 하고, 연동된 봇 자신의 유저 ID를 받아옵니다.
        my_user_id = test_auth_connection()
        
        if my_user_id:
            # 2. 받아온 유저 ID로 프로필 조회를 테스트합니다.
            # (테스트하고 싶은 다른 사용자의 유저 ID(예: 'U12345678')를 직접 넣으셔도 됩니다)
            test_get_user_profile('U0B1Z4F8LCF')
            
            # 3 & 4. 채널 조회 및 메시지 전송 테스트
            test_list_channels()
            # test_send_message(TARGET_CHANNEL, "안녕하세요! 프로필 조회 기능이 추가된 API 테스트 메시지입니다. 🚀")