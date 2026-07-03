import sys
import argparse
import asyncio
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

tests_dir = Path(__file__).resolve().parent
connbot_root = tests_dir.parent
sys.path.insert(0, str(connbot_root))

from app.ml.inference import start_ml_inference, shutdown_ml_inference
from app.agent.router import async_agent_chat
from app.rag.vectordb import init_vectordb
kst = timezone(timedelta(hours=9))

def _kst_now_iso() -> str:
    return datetime.now(kst).strftime("%Y%m%d_%H%M%S")


def load_questions_with_types(txt_path: Path) -> list[dict[str, str]]:
    if not txt_path.exists():
        raise FileNotFoundError(f"질문 파일을 찾을 수 없습니다: {txt_path}")

    lines = txt_path.read_text(encoding="utf-8").splitlines()
    dataset: list[dict[str, str]] = []
    
    for line in lines:
        q_line = line.strip()
        if not q_line:
            continue
        if q_line.startswith("#"):
            continue
            
        # 구분자 '||' 기준으로 질문과 메타 분리
        # 메타: 카테고리 | expected_intent | pass_hint (pipe 구분, 선택)
        if "||" in q_line:
            question, meta = q_line.split("||", 1)
            meta = meta.strip()
            meta_parts = [p.strip() for p in meta.split("|")]
            item: dict[str, str] = {
                "question": question.strip(),
                "type": meta_parts[0] if meta_parts else meta,
            }
            if len(meta_parts) > 1:
                item["expected_intent"] = meta_parts[1]
            if len(meta_parts) > 2:
                item["pass_hint"] = meta_parts[2]
            dataset.append(item)
        else:
            # 예외 처리: 구분자가 없는 경우 기본값 할당
            dataset.append({
                "question": q_line,
                "type": "Unknown"
            })
            
    return dataset


async def run_batch(
    questions: list[dict[str, str]],  # [{"question": "...", "type": "..."}, ...] 구조
    output_jsonl: Path,
    session_prefix: str,
    sleep_sec: float,
) -> None:
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)

    # 배치 프로세스 전체 시작 시간 측정
    batch_started = time.perf_counter()

    with output_jsonl.open("w", encoding="utf-8") as f:
        for idx, item in enumerate(questions, start=1):
            question = item["question"]
            q_type = item["type"]

            session_id = f"{session_prefix}_{idx:04d}"
            
            # 1. 순수 챗봇 추론(API 호출) 시작 시간 측정
            inference_started = time.perf_counter()

            # AI 에이전트 호출 (인공지능 내부 연산)
            (
                answer,
                docs,
                intent,
                _source_docs,
                _links_used,
                _attachments_used,
                gov_attachments,
            ) = await async_agent_chat(question, session_id=session_id)

            # 2. 순수 추론 연산 완료 시간 계산 (Latency)
            latency_sec = round(time.perf_counter() - inference_started, 3)
            
            # 3. 입력 대기 및 처리 등을 포함한 '배치 시작 시점부터 현재 답변 완료까지의 총 경과 시간' 계산
            total_elapsed_sec = round(time.perf_counter() - batch_started, 3)
            
            answer_length = len(answer.strip())

            row = {
                "index": idx,
                "ts_kst": _kst_now_iso(),
                "session_id": session_id,
                "question": question,
                "question_type": q_type,
                "expected_intent": item.get("expected_intent", ""),
                "pass_hint": item.get("pass_hint", ""),
                "intent": intent,
                "docs_count": len(docs) if docs else 0,
                "gov_attachments_count": len(gov_attachments) if gov_attachments else 0,
                "latency_sec": latency_sec,            # 순수 모델 답변 연산 시간
                "total_elapsed_sec": total_elapsed_sec,  # 테스트 시작 후 본 답변을 받기까지 걸린 총 누적 시간
                "answer_length": answer_length,
                "answer": answer,
            }

            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()

            print(
                f"[{idx}/{len(questions)}] type={q_type} | intent={intent} | "
                f"Model Latency={latency_sec}s | Total Elapsed={total_elapsed_sec}s"
            )

            # 질문 사이 디레이(대기 시간)가 설정되어 있다면 수행
            if sleep_sec > 0:
                await asyncio.sleep(sleep_sec)

    # 전체 배치 종료 후 최종 소요 시간 출력
    final_total_time = round(time.perf_counter() - batch_started, 3)
    print(f"🎉 모든 배치가 완료되었습니다. 총 소요 시간: {final_total_time}초")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="test_questions_with_types.txt를 읽어 ConnBot QA 질의/응답을 저장합니다."
    )
    parser.add_argument(
        "--questions",
        type=str,
        default="weak_question2.txt",
        help="질문 텍스트 파일 경로",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=f"qa_results/weak_question_answers_{_kst_now_iso()}.jsonl",
        help="결과 JSONL 저장 경로",
    )
    parser.add_argument(
        "--session-prefix",
        type=str,
        default="qa_weak",
        help="세션 ID prefix",
    )
    parser.add_argument(
        "--sleep-sec",
        type=float,
        default=0.0,
        help="질문 사이 대기 시간(초)",
    )
    args = parser.parse_args()

    questions_path = (tests_dir / args.questions).resolve()
    output_path = (tests_dir / args.output).resolve()

    if not questions_path.exists():
        print(f"질문 파일을 찾을 수 없습니다: {questions_path}")
        return

    print(f"질문 파일: {questions_path}")
    print(f"결과 저장: {output_path}")

    test_dataset = load_questions_with_types(questions_path)

    print("ML inference 워커 시작...")
    await start_ml_inference()

    print("VectorDB 초기화 시작...")
    init_vectordb(force_rebuild=False)
    print("VectorDB 초기화 완료")

    try:
        await run_batch(
            questions=test_dataset,
            output_jsonl=output_path,
            session_prefix=args.session_prefix,
            sleep_sec=args.sleep_sec,
        )
    finally:
        await shutdown_ml_inference()

    print("완료")


if __name__ == "__main__":
    asyncio.run(main())