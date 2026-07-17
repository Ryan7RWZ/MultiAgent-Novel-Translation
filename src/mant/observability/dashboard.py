"""零依赖本地 SSE 监控页：实时追踪 ``data/traces/*.jsonl``。"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from mant.observability.runtime import new_run_id


DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MANT Agent Monitor</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#121a2d;--line:#26324d;--muted:#91a0ba;--text:#edf2ff;--blue:#61a8ff;--green:#55d6a5;--yellow:#ffd166;--red:#ff6b7a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace}header{position:sticky;top:0;z-index:2;display:flex;gap:16px;align-items:center;padding:14px 20px;background:#0b1020ee;border-bottom:1px solid var(--line)}h1{font:700 18px/1.2 system-ui;margin:0}.pill{padding:3px 9px;border:1px solid var(--line);border-radius:999px;color:var(--muted)}.live{color:var(--green);border-color:#2e765e}.grid{display:grid;grid-template-columns:minmax(560px,1.3fr) minmax(360px,.7fr);gap:14px;padding:14px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}.panel h2{font:600 13px system-ui;margin:0;padding:10px 12px;border-bottom:1px solid var(--line);color:var(--muted)}.agents{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;padding:12px}.agent{min-height:94px;padding:10px;border:1px solid var(--line);border-radius:9px;background:#0d1527;cursor:pointer}.agent:hover{background:#15213a}.agent b{font-family:system-ui}.agent .status{margin-top:9px;color:var(--muted)}.agent.running{border-color:var(--blue);box-shadow:0 0 0 1px #61a8ff33}.agent.completed{border-color:#2c735a}.agent.failed{border-color:var(--red)}.metric{display:inline-block;margin:5px 7px 0 0;color:var(--muted);font-size:12px}.stream{height:330px;overflow:auto;white-space:pre-wrap;word-break:break-word;padding:12px;color:#dce7ff}.stream .head{color:var(--blue);font-weight:700}.events{height:590px;overflow:auto}.event{display:grid;grid-template-columns:74px 118px 1fr;gap:7px;padding:7px 10px;border-bottom:1px solid #1d2940}.event:hover{background:#172139}.event .time,.event .who{color:var(--muted)}.event .type{color:#c9d7f1}.event.error .type{color:var(--red)}.event.done .type{color:var(--green)}.summary{display:flex;gap:18px;flex-wrap:wrap;padding:10px 12px}.summary strong{font-size:18px;color:var(--green)}select{margin-left:auto;max-width:340px;background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:7px;padding:6px}@media(max-width:980px){.grid{grid-template-columns:1fr}.agents{grid-template-columns:repeat(2,1fr)}}
input,textarea,button{font:inherit}input,textarea{width:100%;color:var(--text);background:#0d1527;border:1px solid var(--line);border-radius:8px;padding:9px}textarea{min-height:230px;resize:vertical;line-height:1.65}.composer{padding:12px}.fields{display:grid;grid-template-columns:1fr 1fr 130px;gap:9px;margin-bottom:9px}.fields label{color:var(--muted);font-size:12px}.fields input{margin-top:4px}.drop-zone{position:relative}.drop-zone.dragging textarea{border-color:var(--blue);box-shadow:0 0 0 2px #61a8ff44}.actions{display:flex;align-items:center;gap:9px;margin-top:9px}.button,.file-button{display:inline-flex;align-items:center;justify-content:center;min-height:38px;padding:7px 13px;border-radius:8px;border:1px solid var(--line);background:#17233d;color:var(--text);cursor:pointer}.button.primary{background:#2468b5;border-color:#3886d9;font-weight:700}.button:disabled{opacity:.55;cursor:not-allowed}.file-button input{display:none}.counter{margin-left:auto;color:var(--muted);font-size:12px}.job-message{min-height:22px;margin-top:8px;color:var(--muted)}.job-message.error{color:var(--red)}.job-message.success{color:var(--green)}.result{min-height:180px;max-height:480px;overflow:auto;white-space:pre-wrap;word-break:break-word;padding:12px;color:#e8efff}.result-meta{padding:0 12px 10px;color:var(--muted);font-size:12px}@media(max-width:700px){.fields{grid-template-columns:1fr}.actions{flex-wrap:wrap}.counter{margin-left:0}}
</style></head>
<body><header><h1>MANT · 浏览器翻译工作台</h1><span id="connection" class="pill">连接中</span><span id="runStatus" class="pill">等待运行</span><select id="runSelect"><option value="">最新运行</option></select></header>
<main class="grid"><section>
<div class="panel"><h2>输入原文 · 可直接粘贴或拖入 TXT</h2><div class="composer">
<div class="fields"><label>作品 ID<input id="workInput" value="demo_work" maxlength="80"></label><label>章节 ID<input id="chapterInput" placeholder="留空则自动生成" maxlength="80"></label><label>最大返工<input id="reworkInput" type="number" value="2" min="0" max="10"></label></div>
<div id="dropZone" class="drop-zone"><textarea id="sourceText" placeholder="在这里粘贴需要翻译的中文原文，或把 UTF-8 .txt 文件拖到此区域……"></textarea></div>
<div class="actions"><label class="file-button">选择 TXT<input id="fileInput" type="file" accept=".txt,text/plain"></label><button id="translateButton" class="button primary">开始翻译</button><button id="clearButton" class="button">清空</button><span id="charCount" class="counter">0 字符</span></div>
<div id="jobMessage" class="job-message">任务将在后台执行，下方会实时显示每个 Agent 的状态与输出。</div>
</div></div>
<div class="panel" style="margin-top:14px"><h2>运行概览</h2><div class="summary"><span>Run<br><b id="runId">—</b></span><span>作品 / 章节<br><b id="work">—</b></span><span>QA<br><strong id="qa">—</strong></span><span>返工<br><b id="rework">0</b></span></div></div>
<div class="panel" style="margin-top:14px"><h2>Agent 状态</h2><div id="agents" class="agents"></div></div>
<div class="panel" style="margin-top:14px"><h2>LLM 流式输出 · 点击 Agent 可切换</h2><div id="stream" class="stream">等待模型输出…</div></div>
<div class="panel" style="margin-top:14px"><h2>最终译文</h2><div id="resultText" class="result">翻译完成后将在这里显示最终译文。</div><div id="resultMeta" class="result-meta"></div></div></section>
<aside class="panel"><h2>事件时间线</h2><div id="events" class="events"></div></aside></main>
<script>
const $=id=>document.getElementById(id),connection=$('connection'),runStatus=$('runStatus'),runSelect=$('runSelect'),runId=$('runId'),work=$('work'),qa=$('qa'),rework=$('rework'),agents=$('agents'),streamBox=$('stream'),events=$('events'),workInput=$('workInput'),chapterInput=$('chapterInput'),reworkInput=$('reworkInput'),sourceText=$('sourceText'),dropZone=$('dropZone'),fileInput=$('fileInput'),translateButton=$('translateButton'),clearButton=$('clearButton'),charCount=$('charCount'),jobMessage=$('jobMessage'),resultText=$('resultText'),resultMeta=$('resultMeta');
const roles=['orchestrator','terminologist','translator','editor','polisher','qa'];
const roleName={orchestrator:'调度',terminologist:'术语',translator:'翻译',editor:'审校',polisher:'润色',qa:'QA 终审'};
const runs=new Map();let selected='';
function state(id){if(!runs.has(id))runs.set(id,{events:[],agents:{},outputs:{},outputByAgent:{},summary:{}});return runs.get(id)}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function current(){return selected||[...runs.keys()].at(-1)||''}
function ensureOption(id){if(![...runSelect.options].some(o=>o.value===id)){let o=document.createElement('option');o.value=id;o.textContent=id;runSelect.appendChild(o)}if(selected===id)runSelect.value=id}
function ingest(e){const s=state(e.run_id);s.events.push(e);if(s.events.length>800)s.events.shift();ensureOption(e.run_id);const a=e.agent||e.node||'workflow';if(e.event_type==='agent.started')s.agents[a]={status:'running',started:e.timestamp,tier:e.tier,segment:e.segment_id};if(e.event_type==='agent.completed')s.agents[a]={...(s.agents[a]||{}),status:e.payload.ok===false?'failed':'completed',ms:e.metrics.duration_ms};if(e.event_type==='agent.failed')s.agents[a]={...(s.agents[a]||{}),status:'failed'};if(e.event_type==='llm.token'){const id=e.payload.call_id||a;s.outputs[id]=(s.outputs[id]||'')+(e.payload.delta||'');s.outputByAgent[a]=id;s.outputAgent=a;s.outputCall=id}if(e.event_type==='run.started')s.summary={...s.summary,status:'running',work:e.work_id,chapter:e.chapter_id};if(e.event_type==='run.completed')s.summary={...s.summary,status:'completed',...e.payload,ms:e.metrics.duration_ms};if(e.event_type==='run.failed')s.summary={...s.summary,status:'failed',error:e.payload.error};if(!selected||selected===e.run_id)render(e.run_id)}
function showAgent(role){const id=current();if(!id)return;state(id).displayAgent=role;render(id)}
function render(id){if(!id||!runs.has(id))return;const s=runs.get(id);runId.textContent=id;work.textContent=(s.summary.work||'—')+' / '+(s.summary.chapter||'—');qa.textContent=s.summary.qa_verdict?`${s.summary.qa_verdict} (${s.summary.qa_score??0})`:'—';rework.textContent=s.summary.rework_count??0;runStatus.textContent=s.summary.status||'running';runStatus.className='pill '+(s.summary.status==='completed'?'live':'');agents.innerHTML=roles.map(r=>{const a=s.agents[r]||{status:'waiting'};return `<div class="agent ${a.status}" onclick="showAgent('${r}')"><b>${roleName[r]}</b><div class="status">${a.status}</div><span class="metric">${a.tier||''}</span><span class="metric">${a.ms?Math.round(a.ms)+' ms':''}</span><div class="metric">${esc(a.segment||'')}</div></div>`}).join('');const outputAgent=s.displayAgent||s.outputAgent,call=s.outputByAgent[outputAgent]||s.outputCall;streamBox.innerHTML=call?`<span class="head">[${esc(outputAgent)}]</span>\n${esc(s.outputs[call])}`:'等待模型输出…';streamBox.scrollTop=streamBox.scrollHeight;events.innerHTML=s.events.slice(-250).reverse().map(e=>{const cls=/failed/.test(e.event_type)?'error':/completed/.test(e.event_type)?'done':'';const t=(e.timestamp||'').slice(11,19);const who=e.agent||e.node||'workflow';let detail=e.event_type;if(e.event_type==='workflow.route')detail+=' → '+(e.payload.route||'');return `<div class="event ${cls}"><span class="time">${t}</span><span class="who">${esc(who)}</span><span class="type">${esc(detail)}</span></div>`}).join('')}
function setJobMessage(text,kind=''){jobMessage.textContent=text;jobMessage.className='job-message '+kind}
function updateCount(){charCount.textContent=`${sourceText.value.length} 字符`}
async function loadFile(file){if(!file)return;if(!file.name.toLowerCase().endsWith('.txt')&&file.type!=='text/plain'){setJobMessage('请选择 TXT 文本文件。','error');return}try{sourceText.value=await file.text();if(!chapterInput.value)chapterInput.value=file.name.replace(/\.txt$/i,'');updateCount();setJobMessage(`已载入 ${file.name}，共 ${sourceText.value.length} 字符。`,'success')}catch(e){setJobMessage(`读取文件失败：${e.message}`,'error')}}
async function pollJob(id){while(true){await new Promise(resolve=>setTimeout(resolve,1000));try{const response=await fetch(`/api/jobs/${encodeURIComponent(id)}`,{cache:'no-store'}),job=await response.json();if(!response.ok)throw new Error(job.error||'任务查询失败');if(job.status==='completed'){resultText.textContent=job.result_text||'（译文为空）';const m=job.metadata||{};resultMeta.textContent=`输出：${job.output_path} · QA=${m.qa_verdict??'—'} · score=${m.qa_score??'—'}`;setJobMessage('翻译完成。','success');translateButton.disabled=false;return}if(job.status==='failed'){setJobMessage(`翻译失败：${job.error||'未知错误'}`,'error');translateButton.disabled=false;return}setJobMessage(`任务 ${id} 正在运行，请保持页面打开……`)}catch(e){setJobMessage(`任务状态查询失败，稍后自动重试：${e.message}`,'error')}}}
async function startTranslation(){const text=sourceText.value;if(!text.trim()){setJobMessage('请粘贴原文或拖入一个非空 TXT 文件。','error');sourceText.focus();return}translateButton.disabled=true;resultText.textContent='翻译进行中……';resultMeta.textContent='';setJobMessage('正在提交任务……');try{const response=await fetch('/api/translate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,work_id:workInput.value,chapter_id:chapterInput.value,max_rework:Number(reworkInput.value)})}),job=await response.json();if(!response.ok)throw new Error(job.error||'提交失败');selected=job.run_id;ensureOption(job.run_id);runSelect.value=job.run_id;setJobMessage(`任务 ${job.job_id} 已提交，正在启动 Agent……`,'success');pollJob(job.job_id)}catch(e){setJobMessage(`提交失败：${e.message}`,'error');translateButton.disabled=false}}
sourceText.addEventListener('input',updateCount);fileInput.addEventListener('change',()=>loadFile(fileInput.files[0]));clearButton.addEventListener('click',()=>{sourceText.value='';fileInput.value='';resultText.textContent='翻译完成后将在这里显示最终译文。';resultMeta.textContent='';updateCount();sourceText.focus()});translateButton.addEventListener('click',startTranslation);['dragenter','dragover'].forEach(name=>dropZone.addEventListener(name,e=>{e.preventDefault();dropZone.classList.add('dragging')}));['dragleave','drop'].forEach(name=>dropZone.addEventListener(name,e=>{e.preventDefault();dropZone.classList.remove('dragging')}));dropZone.addEventListener('drop',e=>loadFile(e.dataTransfer.files[0]));
runSelect.onchange=()=>{selected=runSelect.value;render(current())};
const source=new EventSource('/events');source.onopen=()=>{connection.textContent='● 实时连接';connection.className='pill live'};source.onerror=()=>{connection.textContent='重连中';connection.className='pill'};source.onmessage=msg=>{try{ingest(JSON.parse(msg.data))}catch(e){console.error(e)}};
fetch('/api/health').then(r=>r.json()).then(h=>{if(h.active_job_id){selected=h.active_job_id;translateButton.disabled=true;setJobMessage(`检测到运行中的任务 ${h.active_job_id}，正在重新连接……`);pollJob(h.active_job_id)}}).catch(()=>{});updateCount();
</script></body></html>"""


class TraceBroker:
    """轮询 JSONL 增量并广播给所有 SSE 客户端。"""

    def __init__(self, trace_dir: str | Path, *, poll_interval: float = 0.2) -> None:
        self.trace_dir = Path(trace_dir)
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self.poll_interval = poll_interval
        self._positions: dict[Path, int] = {}
        self._subscribers: set[queue.Queue[str]] = set()
        self._backlog: deque[str] = deque(maxlen=500)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        for path in sorted(self.trace_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
                for line in lines[-200:]:
                    if self._valid(line):
                        self._backlog.append(line)
                self._positions[path] = path.stat().st_size
            except OSError:
                continue
        self._thread = threading.Thread(target=self._follow, daemon=True)
        self._thread.start()

    @staticmethod
    def _valid(line: str) -> bool:
        try:
            return isinstance(json.loads(line), dict)
        except (json.JSONDecodeError, TypeError):
            return False

    def _broadcast(self, line: str) -> None:
        with self._lock:
            self._backlog.append(line)
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(line)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(line)
                except (queue.Empty, queue.Full):
                    pass

    def _follow(self) -> None:
        while not self._stop.wait(self.poll_interval):
            for path in self.trace_dir.glob("*.jsonl"):
                try:
                    size = path.stat().st_size
                    position = self._positions.get(path, 0)
                    if size < position:
                        position = 0
                    if size == position:
                        continue
                    with path.open("rb") as handle:
                        handle.seek(position)
                        data = handle.read()
                        self._positions[path] = handle.tell()
                    for raw in data.splitlines():
                        line = raw.decode("utf-8", errors="replace")
                        if self._valid(line):
                            self._broadcast(line)
                except OSError:
                    continue

    def subscribe(self) -> queue.Queue[str]:
        subscriber: queue.Queue[str] = queue.Queue(maxsize=1000)
        with self._lock:
            for line in self._backlog:
                try:
                    subscriber.put_nowait(line)
                except queue.Full:
                    break
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[str]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


_SAFE_ID_RE = re.compile(r"[^\w.-]+", re.UNICODE)


def _safe_id(value: Any, *, fallback: str, max_length: int = 80) -> str:
    """把用户提供的作品/章节标识规整为单个安全路径片段。"""
    text = str(value or "").strip()
    text = _SAFE_ID_RE.sub("-", text).strip("-._")
    text = text[:max_length] or fallback
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{number}"
        for prefix in ("COM", "LPT")
        for number in range(1, 10)
    }
    return f"_{text}" if text.upper() in reserved else text


@dataclass
class TranslationJob:
    """浏览器发起的一次后台翻译任务。"""

    job_id: str
    work_id: str
    chapter_id: str
    input_path: Path
    output_path: Path
    metadata_path: Path
    max_rework: int
    status: str = "queued"
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z")
    )
    started_at: str = ""
    completed_at: str = ""
    error: str = ""
    return_code: int | None = None
    result_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def public(self, root: Path) -> dict[str, Any]:
        def relative(path: Path) -> str:
            try:
                return str(path.relative_to(root))
            except ValueError:
                return str(path)

        return {
            "job_id": self.job_id,
            "run_id": self.job_id,
            "work_id": self.work_id,
            "chapter_id": self.chapter_id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "return_code": self.return_code,
            "input_path": relative(self.input_path),
            "output_path": relative(self.output_path),
            "metadata_path": relative(self.metadata_path),
            "result_text": self.result_text,
            "metadata": self.metadata,
        }


class TranslationJobManager:
    """验证浏览器输入，并以正式 CLI 子进程串行执行翻译。"""

    def __init__(
        self,
        *,
        config_path: str | Path = "config/settings.yaml",
        trace_dir: str | Path = "data/traces",
        project_root: str | Path | None = None,
        max_input_chars: int = 200_000,
    ) -> None:
        self.project_root = (
            Path(project_root).resolve()
            if project_root is not None
            else Path(__file__).resolve().parents[3]
        )
        config = Path(config_path)
        traces = Path(trace_dir)
        self.config_path = (
            config.resolve() if config.is_absolute() else (self.project_root / config).resolve()
        )
        self.trace_dir = (
            traces.resolve() if traces.is_absolute() else (self.project_root / traces).resolve()
        )
        self.max_input_chars = max(1, int(max_input_chars))
        self.max_request_bytes = self.max_input_chars * 4 + 16_384
        self._jobs: dict[str, TranslationJob] = {}
        self._active_job_id = ""
        self._process: subprocess.Popen | None = None
        self._lock = threading.RLock()

    @property
    def active_job_id(self) -> str:
        with self._lock:
            return self._active_job_id

    def submit(
        self,
        *,
        text: Any,
        work_id: Any = "demo_work",
        chapter_id: Any = "",
        max_rework: Any = 2,
    ) -> dict[str, Any]:
        source = str(text or "")
        if not source.strip():
            raise ValueError("请输入需要翻译的文本，或拖入一个非空 TXT 文件。")
        if len(source) > self.max_input_chars:
            raise ValueError(
                f"输入过长：{len(source)} 字符，当前上限为 {self.max_input_chars}。"
            )
        try:
            rework_limit = int(max_rework)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_rework 必须是整数。") from exc
        if not 0 <= rework_limit <= 10:
            raise ValueError("max_rework 必须在 0 到 10 之间。")

        safe_work = _safe_id(work_id, fallback="demo_work")
        default_chapter = datetime.now().strftime("web-%Y%m%d-%H%M%S")
        safe_chapter = _safe_id(chapter_id, fallback=default_chapter)
        job_id = new_run_id().replace("run-", "web-", 1)

        with self._lock:
            if self._active_job_id:
                active = self._jobs.get(self._active_job_id)
                if active is not None and active.status in {"queued", "running"}:
                    raise RuntimeError(
                        f"已有任务 {active.job_id} 正在运行，请等待完成后再提交。"
                    )
                self._active_job_id = ""

            input_path = (
                self.project_root
                / "data"
                / "inputs"
                / safe_work
                / safe_chapter
                / f"{job_id}.txt"
            )
            output_dir = (
                self.project_root / "data" / "exports" / "web" / safe_work / safe_chapter
            )
            output_path = output_dir / f"{job_id}.txt"
            metadata_path = output_dir / f"{job_id}.json"
            input_path.parent.mkdir(parents=True, exist_ok=True)
            input_path.write_text(source, encoding="utf-8")
            job = TranslationJob(
                job_id=job_id,
                work_id=safe_work,
                chapter_id=safe_chapter,
                input_path=input_path,
                output_path=output_path,
                metadata_path=metadata_path,
                max_rework=rework_limit,
            )
            self._jobs[job_id] = job
            self._active_job_id = job_id
            thread = threading.Thread(target=self._run, args=(job,), daemon=True)
            thread.start()
            return job.public(self.project_root)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.public(self.project_root) if job is not None else None

    def _run(self, job: TranslationJob) -> None:
        with self._lock:
            job.status = "running"
            job.started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        command = [
            sys.executable,
            "-m",
            "mant.cli",
            "translate-chapter",
            "--config",
            str(self.config_path),
            "--work-id",
            job.work_id,
            "--chapter-id",
            job.chapter_id,
            "--input",
            str(job.input_path),
            "--max-rework",
            str(job.max_rework),
            "--output",
            str(job.output_path),
            "--metadata-output",
            str(job.metadata_path),
            "--trace-dir",
            str(self.trace_dir),
            "--trace",
            "--run-id",
            job.job_id,
        ]
        try:
            with self._lock:
                process = subprocess.Popen(  # noqa: S603 - 固定 argv，无 shell
                    command,
                    cwd=self.project_root,
                )
                self._process = process
            return_code = process.wait()
            with self._lock:
                job.return_code = return_code
                if return_code == 0 and job.output_path.is_file():
                    job.result_text = job.output_path.read_text(encoding="utf-8")
                    if job.metadata_path.is_file():
                        try:
                            metadata = json.loads(
                                job.metadata_path.read_text(encoding="utf-8")
                            )
                            if isinstance(metadata, dict):
                                job.metadata = metadata
                        except (OSError, json.JSONDecodeError):
                            pass
                    job.status = "completed"
                else:
                    job.status = "failed"
                    if not job.error:
                        job.error = f"翻译进程退出码：{return_code}"
        except Exception as exc:  # noqa: BLE001 - 后台任务必须转为可查询状态
            with self._lock:
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {str(exc)[:300]}"
        finally:
            with self._lock:
                self._process = None
                job.completed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                if self._active_job_id == job.job_id:
                    self._active_job_id = ""

    def close(self) -> None:
        """关闭工作台时终止仍在运行的翻译子进程，防止孤儿任务。"""
        with self._lock:
            process = self._process
            active = self._jobs.get(self._active_job_id)
            if active is not None and active.status in {"queued", "running"}:
                active.status = "failed"
                active.error = "监控服务已关闭，翻译任务被终止。"
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _handler_for(broker: TraceBroker, jobs: TranslationJobManager):
    class DashboardHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            path = urlparse(self.path).path
            if path == "/":
                body = DASHBOARD_HTML.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/health":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "translation_enabled": True,
                        "active_job_id": jobs.active_job_id,
                    },
                )
                return
            if path.startswith("/api/jobs/"):
                job_id = unquote(path.removeprefix("/api/jobs/"))
                job = jobs.get(job_id)
                if job is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "任务不存在。"})
                else:
                    self._send_json(HTTPStatus.OK, job)
                return
            if path == "/events":
                self._serve_events()
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            path = urlparse(self.path).path
            if path != "/api/translate":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            content_type = self.headers.get("Content-Type", "")
            if not content_type.lower().startswith("application/json"):
                self._send_json(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    {"error": "请求必须使用 application/json。"},
                )
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "请求体为空。"})
                return
            if length > jobs.max_request_bytes:
                self._send_json(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    {"error": "请求体超过浏览器翻译输入上限。"},
                )
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("请求 JSON 必须是对象。")
                job = jobs.submit(
                    text=payload.get("text"),
                    work_id=payload.get("work_id", "demo_work"),
                    chapter_id=payload.get("chapter_id", ""),
                    max_rework=payload.get("max_rework", 2),
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except RuntimeError as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.ACCEPTED, job)

        def _serve_events(self) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            subscriber = broker.subscribe()
            try:
                while True:
                    try:
                        line = subscriber.get(timeout=15)
                        packet = f"data: {line}\n\n".encode("utf-8")
                    except queue.Empty:
                        packet = b": heartbeat\n\n"
                    self.wfile.write(packet)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                broker.unsubscribe(subscriber)

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return DashboardHandler


def serve_dashboard(
    trace_dir: str | Path = "data/traces",
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    config_path: str | Path = "config/settings.yaml",
    max_input_chars: int = 200_000,
) -> None:
    """阻塞启动本地翻译工作台与监控服务，Ctrl+C 后干净退出。"""
    project_root = Path(__file__).resolve().parents[3]
    trace_path = Path(trace_dir)
    if not trace_path.is_absolute():
        trace_path = project_root / trace_path
    broker = TraceBroker(trace_path)
    broker.start()
    jobs = TranslationJobManager(
        config_path=config_path,
        trace_dir=trace_path,
        project_root=project_root,
        max_input_chars=max_input_chars,
    )
    server = ThreadingHTTPServer((host, int(port)), _handler_for(broker, jobs))
    server.daemon_threads = True
    print(f"[monitor] 监控页：http://{host}:{port}")
    print(f"[monitor] 正在追踪：{trace_path.resolve()}")
    print("[monitor] 按 Ctrl+C 停止。")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        jobs.close()
        broker.close()
