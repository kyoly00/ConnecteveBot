import os
import json

# =========================================================================================
# [파일명] filtering_attachment.py
# 
# [생성 목적 및 배경]
# Confluence 페이지 데이터를 추출하여 JSON으로 저장할 때, 특정 페이지의 'attachments(첨부파일)' 
# 리스트에 다른 페이지에 속해야 할 첨부파일 요소들이 섞여서 함께 저장되는 노이즈 현상이 발견되었습니다.
# 이를 방지하고 RAG/LLM 파이프라인에 정확한 데이터만 제공하기 위해, 
# 실제 해당 페이지의 본문(body_view_html) 내에 첨부파일의 파일명(title)이 언급되어 있는 경우에만 
# 실제 사용된 첨부파일로 간주하고, 본문에 없는 파일은 리스트에서 삭제(필터링)하는 후처리가 필요
# =========================================================================================

def filter_attachments_in_directory(directory_path):
    """
    지정된 디렉토리 내의 JSON 파일을 순회하며 구조에 맞게 첨부파일을 필터링합니다.
    - 구조: Root > page > body_view_html
    - 구조: Root > attachments > [{title: "..."}]
    """
    if not os.path.exists(directory_path):
        print(f"❌ 경로를 찾을 수 없습니다: {directory_path}")
        return

    # 디렉토리 내의 모든 파일 순회
    for filename in os.listdir(directory_path):
        if not filename.endswith('.json'):
            continue
            
        file_path = os.path.join(directory_path, filename)
        
        try:
            # 1. JSON 파일 읽기
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # 2. 알려주신 구조에 맞게 데이터 추출
            page_data = data.get("page", {})
            attachments = data.get("attachments", [])
            
            body_view_html = page_data.get("body_view_html", "")
            
            # 본문이 없거나, 첨부파일 목록이 비어있으면 필터링 패스
            if not body_view_html or not attachments:
                continue
                
            filtered_attachments = []
            removed_count = 0
            
            # 3. 첨부파일(attachments) 필터링 진행
            for attachment in attachments:
                title = attachment.get("title", "")
                
                # 핵심 로직: 첨부파일의 title이 본문 HTML 안에 실제로 등장하는지 검사
                if title and title in body_view_html:
                    filtered_attachments.append(attachment)
                else:
                    removed_count += 1 # 본문에 없는 불필요한 파일 카운팅
            
            # 4. 변경사항이 있는 경우에만 파일 덮어쓰기
            if removed_count > 0:
                # 최상위 Root의 attachments 데이터를 필터링된 배열로 교체
                data["attachments"] = filtered_attachments
                
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                
                print(f"✅ [{filename}] 필터링 완료: 노이즈 데이터 {removed_count}개 삭제, {len(filtered_attachments)}개 유지")
                
        except json.JSONDecodeError:
            print(f"⚠️ JSON 파싱 에러 (파일 손상 의심: {filename})")
        except Exception as e:
            print(f"❌ 처리 중 에러 발생 ({filename}): {e}")

if __name__ == "__main__":
    # 작업 대상 폴더 경로
    TARGET_DIRECTORY = os.path.join("Data", "PolicyPage")
    
    print(f"🚀 [{TARGET_DIRECTORY}] 경로의 첨부파일 노이즈 필터링 작업을 시작합니다...\n")
    filter_attachments_in_directory(TARGET_DIRECTORY)
    print("\n✅ 모든 페이지에 대한 첨부파일 필터링이 완료되었습니다.")