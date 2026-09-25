import asyncio
import sys
from pathlib import Path

from student_agent.cases import load_case_set
from student_agent.cli import _run
from student_agent.contracts import Contracts
from student_agent.submission import package_submission, validate_artifacts

async def main():
    root = Path('.').resolve()
    print('>>> BƯỚC 1: Bắt đầu xử lý 100 cases qua Multi-Agent Workflow...')
    await _run(root, force=True)
    
    print('\n>>> BƯỚC 2: Kiểm tra tính toàn vẹn (day09 validate)...')
    case_set = load_case_set(root)
    contracts = Contracts(root / 'contracts' / 'schemas')
    outputs, trace = validate_artifacts(root, case_set, contracts)
    print(f'OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events hợp lệ 100%!')
    
    print('\n>>> BƯỚC 3: Đóng gói bài nộp (day09 package)...')
    destination = root / 'dist' / 'submission.zip'
    pkg = package_submission(root, destination)
    print(f'\n======================================================')
    print(f'THÀNH CÔNG: Đã tạo file nộp bài tại:')
    print(f'{pkg}')
    print(f'Kích thước file: {pkg.stat().st_size} bytes')
    print(f'======================================================')

if __name__ == '__main__':
    asyncio.run(main())
