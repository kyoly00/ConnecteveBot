import os
import sys
import json
from pathlib import Path

# 프로젝트 루트를 경로에 추가 (필요시)
sys.path.insert(0, str(Path(__file__).parent.resolve()))

from parsers.base import get_parser

def test_parsers():
    test_dir = Path("parsers/test")
    
    if not test_dir.exists():
        print(f"테스트 디렉토리가 없습니다: {test_dir.absolute()}")
        test_dir.mkdir(parents=True, exist_ok=True)
        print("디렉토리를 생성했습니다. 테스트할 문서 파일들을 넣어주세요.")
        return

    files = [f for f in test_dir.iterdir() if f.is_file()]
    
    if not files:
        print(f"{test_dir} 디렉토리에 테스트할 파일이 없습니다.")
        return

    print(f"총 {len(files)}개의 파일을 테스트합니다...\n")

    for file_path in files:
        print("="*60)
        print(f"📄 파일: {file_path.name}")
        ext = file_path.suffix.lower()
        
        parser = get_parser(ext)
        if not parser:
            print(f"⚠️ [{ext}] 확장자를 지원하는 파서를 찾을 수 없습니다. (스킵)")
            continue
            
        try:
            print(f"🚀 파싱 시작... (사용된 파서: {parser.__class__.__name__})")
            result = parser.parse(file_path)
            
            print("-" * 30)
            print("[ 메타데이터 ]")
            print(json.dumps(result.metadata, indent=2, ensure_ascii=False))
                
            print(f"\n- 페이지 수: {result.page_count}")
            print(f"- 테이블 수: {len(result.tables)}")
            print(f"- 이미지/텍스트 요소 수: {len(result.images_text)}")
            print(f"- 파싱 메소드: {result.parse_method}")
            print(f"- 신뢰도: {result.confidence}")
            
            print("\n[ 추출된 텍스트 프리뷰 (최대 500자) ]")
            text_preview = result.text[:500].replace('\n', ' ')
            print(f"{text_preview}...")
            if len(result.text) > 500:
                print(f"...(전체 텍스트 길이: {len(result.text)}자)")
            elif len(result.text) == 0:
                print("(텍스트가 없습니다)")
                
        except Exception as e:
            print(f"❌ 파싱 중 에러 발생: {e}")

if __name__ == "__main__":
    test_parsers()