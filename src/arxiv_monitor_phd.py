#!/usr/bin/env python3
"""
arXiv AI Infra 论文监控 - 博士生增强版
新增功能:
- 语义相似度评分
- 性能指标提取
- 研究相关度分析
- 代码仓库检测
"""

import argparse
import warnings
from datetime import datetime, timedelta
import fcntl
import json
import re
import tempfile
import time
import os
import sys

# urllib3 raises this at import time on the system Python linked against LibreSSL.
warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL 1\.1\.1\+.*",
    category=Warning,
)

import requests
from lxml import etree

DEFAULT_USER_AGENT = os.environ.get(
    "DAILY_PAPER_BRIEF_USER_AGENT",
    "DailyPaperBrief/1.0 (independent research tool)",
)
MAX_FEED_BYTES = 10 * 1024 * 1024

try:
    sys.stdout.reconfigure(line_buffering=True)
except AttributeError:
    pass


def atomic_write(path, content, *, binary=False):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        dir=os.path.dirname(path),
    )
    try:
        mode = "wb" if binary else "w"
        kwargs = {} if binary else {"encoding": "utf-8"}
        with os.fdopen(descriptor, mode, **kwargs) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def resolve_config_path(config_path):
    """Resolve config paths relative to the repository root so cron cwd does not matter."""
    if os.path.isabs(config_path):
        return config_path
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, config_path)


class SingleRunLock:
    """Prevent concurrent runs from racing on shared report and pushed-id files."""

    def __init__(self, lock_path):
        self.lock_path = lock_path
        self._fd = None

    def acquire(self):
        self._fd = open(self.lock_path, 'a+', encoding='utf-8')
        try:
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._fd.close()
            self._fd = None
            return False

        self._fd.seek(0)
        self._fd.truncate()
        self._fd.write(str(os.getpid()))
        self._fd.flush()
        return True

    def release(self):
        if self._fd is None:
            return
        try:
            self._fd.seek(0)
            self._fd.truncate()
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
        finally:
            self._fd.close()
            self._fd = None


def is_lock_available(lock_path):
    """Check whether another process still holds the monitor lock."""
    probe = SingleRunLock(lock_path)
    if probe.acquire():
        probe.release()
        return True
    return False


def wait_for_existing_run(base_dir, lock_path, timeout_seconds=900, poll_seconds=5):
    """Wait for the active run to finish or for today's report to appear."""
    report_path = os.path.join(
        base_dir, f"arxiv_daily_{datetime.now().strftime('%Y%m%d')}.md"
    )
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        if os.path.exists(report_path) and os.path.getsize(report_path) > 0:
            return "report", report_path
        if is_lock_available(lock_path):
            return "released", None
        time.sleep(poll_seconds)

    return "timeout", None

class ArxivMonitorPhD:
    def __init__(self, config_path="arxiv-monitor-config-phd.json", redo_today=False):
        """初始化监控器"""
        self.config_path = os.path.abspath(resolve_config_path(config_path))
        self.base_dir = os.path.dirname(self.config_path)

        with open(self.config_path, 'r', encoding='utf-8') as f:
            self.config = json.load(f)
        categories = self.config.get("categories")
        if (
            not isinstance(categories, list)
            or not 1 <= len(categories) <= 64
            or any(
                not isinstance(value, str)
                or not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*", value)
                for value in categories
            )
        ):
            raise ValueError("categories must contain 1-64 valid arXiv category identifiers")
        max_papers = int(self.config.get("max_papers_per_day", 18))
        if not 1 <= max_papers <= 24:
            raise ValueError("max_papers_per_day must be between 1 and 24")
        
        self.base_url = "https://export.arxiv.org/api/query"
        self.papers = []
        self.redo_today = bool(redo_today)
        self.request_delay_seconds = max(
            0.0,
            float(self.config.get("request_delay_seconds", 3.0)),
        )
        self.pushed_ids_file = os.path.join(self.base_dir, "arxiv_pushed_ids.json")
        self.pushed_ids = self._load_pushed_ids()

        self.topics = self._load_topics()
        self._topics_by_id = {topic['id']: topic for topic in self.topics}
        self.topic_labels = {topic['id']: topic['label'] for topic in self.topics}
        self._topic_order = self._resolve_topic_order()

    @staticmethod
    def _read_bounded_response(response, max_bytes=MAX_FEED_BYTES):
        announced = response.headers.get("content-length")
        if announced:
            try:
                if int(announced) > max_bytes:
                    raise ValueError(f"response exceeds {max_bytes} bytes")
            except ValueError as exc:
                if str(exc).startswith("response exceeds"):
                    raise
                raise ValueError("invalid response content-length") from exc
        chunks = []
        total = 0
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"response exceeds {max_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    def _fetch_xml(self, url, *, params=None):
        with requests.get(
            url,
            params=params,
            timeout=(10, 20),
            headers={"User-Agent": DEFAULT_USER_AGENT},
            stream=True,
            allow_redirects=False,
        ) as response:
            if 300 <= response.status_code < 400:
                raise requests.RequestException(
                    f"refusing redirect from fixed arXiv endpoint: {url}"
                )
            response.raise_for_status()
            payload = self._read_bounded_response(response)
        if self.request_delay_seconds:
            time.sleep(self.request_delay_seconds)
        parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
        return etree.fromstring(payload, parser=parser)

    def _artifact_path(self, filename):
        """将相对产物路径固定到配置所在目录，避免受 cwd 影响。"""
        if os.path.isabs(filename):
            return filename
        return os.path.join(self.base_dir, filename)

    @staticmethod
    def _paper_arxiv_id(paper):
        """Return the normalized arXiv id from a paper URL or RSS id string."""
        source = paper.get('url', '') if isinstance(paper, dict) else str(paper)
        value = str(source or '').strip()
        if not value:
            return ''

        value = value.split('?')[0].split('#')[0].rstrip('/')
        value = re.sub(r'^https?://[^/]+/(?:abs|pdf)/', '', value)
        value = re.sub(r'\.pdf$', '', value)

        # RSS ids can look like oai:arXiv.org:2604.26039v1.
        match = re.search(r'(\d{4}\.\d{4,5}(?:v\d+)?)$', value)
        if match:
            return match.group(1)

        return value.rsplit('/', 1)[-1].rsplit(':', 1)[-1]

    @staticmethod
    def _paper_arxiv_url(arxiv_id):
        """Build the canonical abstract URL for a normalized arXiv id."""
        return f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""

    @staticmethod
    def _base_arxiv_id(arxiv_id):
        """Collapse versioned ids so v2 does not get pushed as a new paper."""
        return re.sub(r'v\d+$', '', arxiv_id)

    def _save_selected_papers(self, papers):
        papers_to_expand = self._artifact_path('papers_to_expand.json')
        payload = {
            'papers': papers,
            'topic_labels': self.topic_labels,
        }
        atomic_write(
            papers_to_expand,
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        )
        print(f"已保存 {len(papers)} 篇待扩展论文到 {papers_to_expand}")

    def _archive_today_report(self):
        report_path = self._artifact_path(
            f"arxiv_daily_{datetime.now().strftime('%Y%m%d')}.md"
        )
        if os.path.exists(report_path):
            archived_path = (
                f"{report_path}.stale-{datetime.now().strftime('%H%M%S')}"
            )
            os.replace(report_path, archived_path)
            print(f"已归档旧的今日报告: {archived_path}")
    
    def _load_pushed_ids(self):
        """加载已推送过的论文ID"""
        if os.path.exists(self.pushed_ids_file):
            try:
                with open(self.pushed_ids_file, 'r') as f:
                    data = json.load(f)
                # 只保留最近30天的记录，防止文件无限增长
                cutoff = (datetime.now() - timedelta(days=30)).isoformat()
                return {k: v for k, v in data.items() if v >= cutoff}
            except Exception:
                return {}
        return {}
    
    def _save_pushed_ids(self):
        """保存已推送的论文ID"""
        try:
            atomic_write(
                self.pushed_ids_file,
                json.dumps(self.pushed_ids, indent=2) + "\n",
            )
        except Exception as e:
            print(f"保存推送记录失败: {e}")
    
    def _mark_as_pushed(self, papers):
        """标记论文为已推送"""
        now = datetime.now().isoformat()
        for paper in papers:
            arxiv_id = self._paper_arxiv_id(paper)
            if arxiv_id:
                self.pushed_ids[arxiv_id] = now
        self._save_pushed_ids()
    
    def _is_already_pushed(self, paper):
        """检查论文是否已推送过"""
        arxiv_id = self._paper_arxiv_id(paper)
        if not arxiv_id:
            return False
        base_id = self._base_arxiv_id(arxiv_id)
        for pushed_id, pushed_at in self.pushed_ids.items():
            if self._base_arxiv_id(pushed_id) != base_id:
                continue
            if self.redo_today and self._timestamp_is_today(pushed_at):
                continue
            return True
        return False

    @staticmethod
    def _timestamp_is_today(value):
        """Return whether an ISO timestamp falls on today in its recorded zone."""
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except (TypeError, ValueError):
            return False
        now = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
        return parsed.date() == now.date()
    
    def fetch_papers(self, days=1):
        """获取最近N天的论文，API失败时自动回退到RSS"""
        all_papers = self._fetch_papers_api(days)
        
        if len(all_papers) == 0:
            print("  API 未获取到论文，尝试 RSS feed 备用方案...")
            all_papers = self._fetch_papers_rss(days)
        
        self.papers = all_papers
        print(f"共抓取 {len(all_papers)} 篇论文")
        return all_papers
    
    def _fetch_papers_api(self, days):
        """通过 arXiv API 获取论文"""
        all_papers = []
        
        print(f"正在通过 API 抓取论文 ({', '.join(self.config['categories'])})...")
        
        # 合并查询
        categories_query = " OR ".join([f"cat:{cat}" for cat in self.config['categories']])
        params = {
            "search_query": categories_query,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": 300
        }
        
        max_retries = 3
        for retry in range(max_retries):
            try:
                root = self._fetch_xml(self.base_url, params=params)
                ns = {'atom': 'http://www.w3.org/2005/Atom'}
                
                for entry in root.findall('atom:entry', ns):
                    paper = self._parse_entry(entry, ns)
                    
                    if self._is_recent(paper['submitted_date'], days):
                        all_papers.append(paper)
                
                print(f"  API 成功获取 {len(all_papers)} 篇相关论文")
                break
                    
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 429:
                    wait_time = (retry + 1) * 10
                    print(f"  速率限制,等待 {wait_time} 秒后重试...")
                    time.sleep(wait_time)
                else:
                    print(f"  抓取失败: {e}")
                    if retry == max_retries - 1:
                        print(f"  API 已达最大重试次数")
                    break
            except Exception as e:
                print(f"  抓取失败: {e}")
                if retry < max_retries - 1:
                    wait_time = (retry + 1) * 5
                    print(f"  等待 {wait_time} 秒后重试...")
                    time.sleep(wait_time)
                else:
                    print(f"  API 已达最大重试次数")
                    break
        
        return all_papers
    
    def _fetch_papers_rss(self, days):
        """通过 arXiv RSS/Atom feed 获取论文（API 的备用方案）"""
        all_papers = []
        seen_urls = set()
        
        for category in self.config['categories']:
            rss_url = f"https://rss.arxiv.org/atom/{category}"
            print(f"  正在从 RSS 抓取 {category}...")
            
            try:
                root = self._fetch_xml(rss_url)
                ns = {'atom': 'http://www.w3.org/2005/Atom'}
                
                count = 0
                for entry in root.findall('atom:entry', ns):
                    paper = self._parse_rss_entry(entry, ns)
                    if paper and paper['url'] not in seen_urls:
                        if self._is_recent(paper['submitted_date'], days):
                            all_papers.append(paper)
                            seen_urls.add(paper['url'])
                            count += 1
                
                print(f"    {category}: {count} 篇")
            except Exception as e:
                print(f"    {category} RSS 抓取失败: {e}")
                continue
        
        print(f"  RSS 共获取 {len(all_papers)} 篇论文（去重后）")
        return all_papers
    
    def _parse_rss_entry(self, entry, ns):
        """解析 RSS feed 的论文条目"""
        try:
            paper = {}
            
            title = entry.find('atom:title', ns)
            paper['title'] = title.text.strip().replace('\n', ' ') if title is not None and title.text else ""
            if not paper['title']:
                return None
            
            authors = []
            for author in entry.findall('atom:author', ns):
                name = author.find('atom:name', ns)
                if name is not None and name.text:
                    authors.append(name.text.strip())
            paper['authors'] = authors
            
            summary = entry.find('atom:summary', ns)
            paper['abstract'] = summary.text.strip().replace('\n', ' ') if summary is not None and summary.text else ""
            
            # RSS 中 id 可能是 URL、arxiv ID，或 oai:arXiv.org:<id>
            entry_id = entry.find('atom:id', ns)
            if entry_id is not None and entry_id.text:
                arxiv_id = self._paper_arxiv_id(entry_id.text)
                paper['url'] = self._paper_arxiv_url(arxiv_id) or entry_id.text.strip()
            else:
                # 尝试从 link 标签获取
                link = entry.find('atom:link', ns)
                raw_url = link.get('href', '') if link is not None else ""
                arxiv_id = self._paper_arxiv_id(raw_url)
                paper['url'] = self._paper_arxiv_url(arxiv_id) or raw_url
            
            if not paper['url']:
                return None
            
            # 日期
            published = entry.find('atom:published', ns)
            updated = entry.find('atom:updated', ns)
            date_elem = published if published is not None else updated
            if date_elem is not None and date_elem.text:
                paper['submitted_date'] = datetime.fromisoformat(date_elem.text.strip().replace('Z', '+00:00'))
            else:
                paper['submitted_date'] = datetime.now()
            
            # 分类
            categories = []
            for cat in entry.findall('atom:category', ns):
                term = cat.get('term')
                if term:
                    categories.append(term)
            paper['categories'] = categories
            
            paper['code_url'] = self._extract_code_url(paper['abstract'])
            paper['metrics'] = self._extract_metrics(paper['abstract'])
            
            return paper
        except Exception as e:
            print(f"    解析 RSS 条目失败: {e}")
            return None
    
    def _parse_entry(self, entry, ns):
        """解析单篇论文XML"""
        paper = {}
        
        title = entry.find('atom:title', ns)
        paper['title'] = title.text.strip().replace('\n', ' ') if title is not None else ""
        
        authors = []
        for author in entry.findall('atom:author', ns):
            name = author.find('atom:name', ns)
            if name is not None:
                authors.append(name.text.strip())
        paper['authors'] = authors
        
        summary = entry.find('atom:summary', ns)
        paper['abstract'] = summary.text.strip().replace('\n', ' ') if summary is not None else ""
        
        raw_url = entry.find('atom:id', ns).text if entry.find('atom:id', ns) is not None else ""
        arxiv_id = self._paper_arxiv_id(raw_url)
        paper['url'] = self._paper_arxiv_url(arxiv_id) or raw_url
        
        published = entry.find('atom:published', ns)
        if published is not None:
            paper['submitted_date'] = datetime.fromisoformat(published.text.replace('Z', '+00:00'))
        else:
            paper['submitted_date'] = datetime.now()
        
        categories = []
        for cat in entry.findall('atom:category', ns):
            term = cat.get('term')
            if term:
                categories.append(term)
        paper['categories'] = categories
        
        # 提取代码链接
        paper['code_url'] = self._extract_code_url(paper['abstract'])
        
        # 提取性能指标
        paper['metrics'] = self._extract_metrics(paper['abstract'])
        
        return paper
    
    def _extract_code_url(self, text):
        """提取代码仓库链接"""
        patterns = [
            r'github\.com/[\w-]+/[\w-]+',
            r'huggingface\.co/[\w-]+/[\w-]+',
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return f"https://{match.group()}"
        return None
    
    def _extract_metrics(self, text):
        """提取性能指标"""
        metrics = {}
        
        # Speedup: 2.5x, 10x faster
        speedup_patterns = [
            r'(\d+\.?\d*)[x×]\s*(?:faster|speedup)',
            r'speedup\s+of\s+(\d+\.?\d*)[x×]?',
        ]
        for pattern in speedup_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                metrics['speedup'] = match.group(1) + 'x'
                break
        
        # Throughput: 100 tokens/s, 5000 req/s
        throughput_patterns = [
            r'(\d+\.?\d*)\s*(?:tokens?|requests?)/s',
            r'throughput\s+of\s+(\d+\.?\d*)',
        ]
        for pattern in throughput_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                metrics['throughput'] = match.group(1)
                break
        
        # Latency: 50ms, 100 microseconds
        latency_patterns = [
            r'(\d+\.?\d*)\s*(?:ms|microseconds?|μs)',
            r'latency\s+(?:of\s+)?(\d+\.?\d*)',
        ]
        for pattern in latency_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                metrics['latency'] = match.group(1)
                break
        
        # Memory: 30% reduction, 50GB
        memory_patterns = [
            r'(\d+)%\s*(?:memory|RAM)\s*(?:reduction|savings?)',
            r'(\d+\.?\d*)\s*GB\s*memory',
        ]
        for pattern in memory_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                metrics['memory'] = match.group(1)
                break
        
        return metrics
    
    def _is_recent(self, submitted_date, days):
        """检查是否在时间范围内"""
        cutoff = datetime.now(submitted_date.tzinfo) - timedelta(days=days)
        return submitted_date >= cutoff
    
    def filter_papers(self):
        """根据配置筛选论文（含去重）"""
        filtered = []
        seen_ids = set()
        skipped_pushed = 0
        skipped_duplicate = 0
        
        print(f"正在筛选论文...")
        for paper in self.papers:
            arxiv_id = self._paper_arxiv_id(paper)
            base_id = self._base_arxiv_id(arxiv_id)
            if base_id in seen_ids:
                skipped_duplicate += 1
                continue
            seen_ids.add(base_id)

            # 去重：跳过已推送过的论文
            if self._is_already_pushed(paper):
                skipped_pushed += 1
                continue
            
            score, score_details = self._calculate_score_detailed(paper)
            
            if score >= self.config.get('min_score', 5.0):
                paper['relevance_score'] = score
                paper['score_details'] = score_details
                filtered.append(paper)
        
        if skipped_pushed > 0:
            print(f"  跳过 {skipped_pushed} 篇已推送论文")
        if skipped_duplicate > 0:
            print(f"  跳过 {skipped_duplicate} 篇重复论文")
        
        filtered.sort(key=lambda x: x['relevance_score'], reverse=True)
        
        max_papers = self.config.get('max_papers_per_day', 10)
        selected = self._select_balanced_papers(filtered, max_papers)
        
        # 保存筛选后的论文
        self._save_selected_papers(selected)
        
        return selected

    def _load_topics(self):
        """Load scoring topics from config, converting legacy keyword lists if needed."""
        raw_topics = self.config.get('topics')
        if raw_topics is None:
            topics = [
                self._normalize_topic(raw)
                for raw in self._legacy_topics_from_keywords()
            ]
            # Legacy configs tolerate missing keyword lists; skip such topics.
            topics = [
                topic for topic in topics
                if all(group['terms'] for group in topic['term_groups'])
            ]
        else:
            if not isinstance(raw_topics, list) or not raw_topics:
                raise ValueError("topics must be a non-empty array")
            topics = [self._normalize_topic(raw) for raw in raw_topics]
            for topic in topics:
                for group in topic['term_groups']:
                    if not group['terms']:
                        raise ValueError(
                            f"topic {topic['id']} group {group['name']} must list terms"
                        )
        ids = [topic['id'] for topic in topics]
        if len(set(ids)) != len(ids):
            raise ValueError("topic ids must be unique")
        return topics

    def _legacy_topics_from_keywords(self):
        """Convert pre-topics keyword lists into equivalent topic definitions."""
        keywords = self.config.get('keywords', {})
        weights = self.config.get('scoring_weights', {})
        return [
            {
                'id': 'ai_infra',
                'label': 'AI Infra',
                'base': 4.0,
                'cap': 8.0,
                'score_weight': weights.get('ai_infra_match', 0.35),
                'term_groups': [
                    {
                        'name': 'ai_terms',
                        'terms': keywords.get('ai_infra_ai_terms', []),
                        'cap': 3,
                        'weight': 0.75,
                    },
                    {
                        'name': 'system_terms',
                        'terms': keywords.get('ai_infra_system_terms', []),
                        'cap': 5,
                        'weight': 0.60,
                    },
                ],
                'require_all_groups': True,
                'strong_rule': {'title_group': 'system_terms'},
                'title_all_groups_bonus': 1.0,
                'quota': None,
            },
            {
                'id': 'hpc_systems',
                'label': 'HPC Systems',
                'base': 3.5,
                'cap': 8.0,
                'score_weight': weights.get('hpc_match', 0.20),
                'term_groups': [
                    {
                        'name': 'anchors',
                        'terms': keywords.get('hpc_systems_anchor', []),
                        'cap': 3,
                        'weight': 0.9,
                    },
                    {
                        'name': 'mechanisms',
                        'terms': keywords.get('hpc_systems_mechanisms', []),
                        'cap': 5,
                        'weight': 0.65,
                    },
                ],
                'require_all_groups': True,
                'strong_rule': {'title_any': True},
                'title_all_groups_bonus': 0.75,
                'quota': {'cap': 4, 'divisor': 4},
            },
            {
                'id': 'ai4sci_infra',
                'label': 'AI4Sci Infra',
                'base': 3.0,
                'cap': 8.0,
                'score_weight': weights.get('ai4sci_match', 0.10),
                'term_groups': [
                    {
                        'name': 'domains',
                        'terms': keywords.get('ai4sci_domains', []),
                        'cap': 3,
                        'weight': 1.0,
                    },
                    {
                        'name': 'infra',
                        'terms': keywords.get('ai4sci_infra', []),
                        'cap': 4,
                        'weight': 0.75,
                    },
                ],
                'require_all_groups': True,
                'strong_rule': None,
                'title_all_groups_bonus': 0.0,
                'quota': {'cap': 3, 'divisor': 6},
            },
        ]

    @staticmethod
    def _normalize_topic(raw):
        """Normalize one topic definition and fill in defaults."""
        if not isinstance(raw, dict):
            raise ValueError("each topic must be an object")
        topic_id = str(raw.get('id') or '').strip()
        if not re.fullmatch(r'[a-z0-9_]+', topic_id):
            raise ValueError(f"invalid topic id: {topic_id!r}")

        groups = []
        group_names = set()
        for group in raw.get('term_groups') or []:
            if not isinstance(group, dict):
                raise ValueError(f"topic {topic_id} term groups must be objects")
            name = str(group.get('name') or '').strip()
            if not name or name in group_names:
                raise ValueError(f"topic {topic_id} has duplicate or empty group name")
            group_names.add(name)
            terms = [
                str(term) for term in group.get('terms') or [] if str(term).strip()
            ]
            groups.append({
                'name': name,
                'terms': terms,
                'cap': int(group.get('cap', 3)),
                'weight': float(group.get('weight', 1.0)),
            })
        if not groups:
            raise ValueError(f"topic {topic_id} must define at least one term group")

        strong_rule = raw.get('strong_rule')
        if strong_rule is not None:
            if not isinstance(strong_rule, dict):
                raise ValueError(f"topic {topic_id} strong_rule must be an object")
            title_group = strong_rule.get('title_group')
            if title_group is not None and title_group not in group_names:
                raise ValueError(
                    f"topic {topic_id} strong_rule references unknown group {title_group!r}"
                )

        quota = raw.get('quota')
        if quota is None:
            normalized_quota = None
        elif isinstance(quota, dict):
            normalized_quota = {
                'cap': int(quota['cap']),
                'divisor': max(1, int(quota.get('divisor', 1))),
            }
        else:
            normalized_quota = {'cap': int(quota), 'divisor': 1}

        return {
            'id': topic_id,
            'label': str(raw.get('label') or topic_id),
            'base': float(raw.get('base', 0.0)),
            'cap': float(raw.get('cap', 8.0)),
            'score_weight': float(raw.get('score_weight', 0.2)),
            'term_groups': groups,
            'require_all_groups': bool(raw.get('require_all_groups', True)),
            'strong_rule': strong_rule,
            'title_all_groups_bonus': float(raw.get('title_all_groups_bonus', 0.0)),
            'quota': normalized_quota,
        }

    def _resolve_topic_order(self):
        """Topic priority defaults to the config order of the topics array."""
        configured = self.config.get('topic_priority')
        ids = [topic['id'] for topic in self.topics]
        if not configured:
            return ids
        order = [topic_id for topic_id in configured if topic_id in self._topics_by_id]
        order.extend(topic_id for topic_id in ids if topic_id not in order)
        return order

    def _is_topic_paper(self, paper, topic_id):
        """Return whether the paper scores above zero for the given topic."""
        details = paper.get('score_details', {}).get(topic_id, {})
        return bool(details.get('score', 0) > 0)

    def _is_ai4sci_paper(self, paper):
        """Return whether science-domain and infrastructure signals co-occur."""
        return self._is_topic_paper(paper, 'ai4sci_infra')

    def _is_ai_infra_paper(self, paper):
        """Return whether AI/model and systems signals co-occur."""
        return self._is_topic_paper(paper, 'ai_infra')

    def _is_hpc_paper(self, paper):
        """Return whether standalone HPC anchors and systems mechanisms co-occur."""
        return self._is_topic_paper(paper, 'hpc_systems')

    def _paper_primary_topic(self, paper):
        """Assign the first matching configured topic in priority order."""
        for topic_id in self._topic_order:
            if self._is_topic_paper(paper, topic_id):
                return topic_id
        return 'other'

    def _topic_priority_rank(self, paper):
        topic = self._paper_primary_topic(paper)
        try:
            return len(self._topic_order) - self._topic_order.index(topic)
        except ValueError:
            return 0

    def _select_balanced_papers(self, filtered, max_papers):
        """Select by topic priority first, then relevance within each topic."""
        if max_papers <= 0:
            return []

        ordered = sorted(
            filtered,
            key=lambda paper: (
                self._topic_priority_rank(paper),
                paper.get('relevance_score', 0),
            ),
            reverse=True,
        )

        selected = []
        selected_ids = set()

        def paper_base_id(paper):
            return self._base_arxiv_id(self._paper_arxiv_id(paper))

        priority_topics = [self._topics_by_id[tid] for tid in self._topic_order]
        grouped = {topic['id']: [] for topic in priority_topics}
        grouped['other'] = []
        for paper in ordered:
            primary = self._paper_primary_topic(paper)
            grouped[primary if primary in grouped else 'other'].append(paper)

        def effective_quota(topic):
            quota = topic.get('quota')
            if quota is None:
                return None
            return min(
                len(grouped[topic['id']]),
                min(quota['cap'], max(1, max_papers // quota['divisor'])),
            )

        reserved = sum(
            effective_quota(topic) or 0
            for topic in priority_topics
            if topic.get('quota') is not None
        )
        elastic_pool = max(0, max_papers - reserved)

        def add_papers(candidates, limit):
            for paper in candidates:
                if limit <= 0 or len(selected) >= max_papers:
                    break
                base_id = paper_base_id(paper)
                if not base_id or base_id in selected_ids:
                    continue
                paper['primary_topic'] = self._paper_primary_topic(paper)
                selected.append(paper)
                selected_ids.add(base_id)
                limit -= 1

        for topic in priority_topics:
            quota = effective_quota(topic)
            if quota is None:
                before = len(selected)
                add_papers(grouped[topic['id']], elastic_pool)
                elastic_pool -= len(selected) - before
            else:
                add_papers(grouped[topic['id']], quota)
        if len(selected) < max_papers:
            add_papers(ordered, max_papers - len(selected))
        return selected
    
    def _calculate_score_detailed(self, paper):
        """详细评分并返回分数细节"""
        score = 0.0
        details = {}
        
        title_lower = paper['title'].lower()
        abstract_lower = paper['abstract'].lower()
        
        # 1. 关键词匹配
        keyword_score = 0.0
        matched_keywords = []
        must_include = self.config.get('keywords', {}).get('must_include', [])
        for keyword in must_include:
            keyword_lower = keyword.lower()
            if keyword_lower in title_lower:
                keyword_score += 3.0
                matched_keywords.append(f"{keyword}(title)")
            elif keyword_lower in abstract_lower:
                keyword_score += 2.0
                matched_keywords.append(f"{keyword}(abstract)")
        
        # 排除关键词
        exclude = self.config.get('keywords', {}).get('exclude', [])
        for keyword in exclude:
            if keyword.lower() in title_lower or keyword.lower() in abstract_lower:
                return 0.0, {'excluded': keyword}
        
        details['keywords'] = {'score': keyword_score, 'matched': matched_keywords[:5]}

        topic_weighted_scores = []
        for topic in self.topics:
            topic_score, topic_details = self._calculate_topic_match(paper, topic)
            details[topic['id']] = {'score': topic_score, **topic_details}
            topic_weighted_scores.append(topic_score * topic['score_weight'])
        
        # 2. 作者匹配
        author_score = 0.0
        matched_authors = []
        priority_authors = self.config.get('authors', {}).get('priority', [])
        for author in paper['authors']:
            for priority_author in priority_authors:
                if priority_author.lower() in author.lower():
                    author_score += 2.5
                    matched_authors.append(author)
                    break
        details['authors'] = {'score': author_score, 'matched': matched_authors}
        
        # 3. 机构匹配
        institution_score = 0.0
        matched_institutions = []
        institutions = self.config.get('institutions', [])
        for author in paper['authors']:
            for institution in institutions:
                if institution.lower() in author.lower():
                    institution_score += 1.5
                    matched_institutions.append(institution)
                    break
        details['institutions'] = {'score': institution_score, 'matched': matched_institutions}
        
        # 4. 时效性
        recency_score = 0.0
        hours_ago = (datetime.now(paper['submitted_date'].tzinfo) - paper['submitted_date']).total_seconds() / 3600
        if hours_ago < 24:
            recency_score = 1.0
        elif hours_ago < 72:
            recency_score = 0.5
        details['recency'] = {'score': recency_score, 'hours_ago': int(hours_ago)}
        
        # 5. 研究相关度 (基于研究profile)
        research_score = self._calculate_research_relevance(paper)
        details['research_relevance'] = {'score': research_score}
        
        # 应用权重
        weights = self.config.get('scoring_weights', {
            'keyword_match': 0.20,
            'author_match': 0.10,
            'institution_match': 0.05,
            'recency': 0.05,
            'research_relevance': 0.15
        })

        score = (
            keyword_score * weights.get('keyword_match', 0.20) +
            author_score * weights.get('author_match', 0.10) +
            institution_score * weights.get('institution_match', 0.05) +
            recency_score * weights.get('recency', 0.05) +
            research_score * weights.get('research_relevance', 0.15) +
            sum(topic_weighted_scores)
        )

        return min(score, 10.0), details

    def _calculate_topic_match(self, paper, topic):
        """Score a paper against one configured topic definition."""
        title = paper['title'].lower()
        text = (paper['title'] + ' ' + paper['abstract']).lower()

        matched = {}
        title_match = {}
        for group in topic['term_groups']:
            hits = [term for term in group['terms'] if self._term_in_text(term, text)]
            matched[group['name']] = hits
            title_match[group['name']] = any(
                self._term_in_text(term, title) for term in hits
            )

        group_names = [group['name'] for group in topic['term_groups']]
        if topic['require_all_groups']:
            eligible = all(matched[name] for name in group_names)
        else:
            eligible = any(matched[name] for name in group_names)

        if eligible:
            candidate_score = topic['base'] + sum(
                min(len(matched[group['name']]), group['cap']) * group['weight']
                for group in topic['term_groups']
            )
            if topic['title_all_groups_bonus'] and all(title_match.values()):
                candidate_score += topic['title_all_groups_bonus']
        else:
            candidate_score = 0.0

        strong_rule = topic.get('strong_rule')
        if strong_rule is None:
            strong_match = eligible
        elif strong_rule.get('title_group') is not None:
            strong_match = eligible and title_match.get(strong_rule['title_group'], False)
        else:  # {"title_any": true}
            strong_match = eligible and any(title_match.values())

        score = candidate_score if strong_match else 0.0

        details = {name: hits[:6] for name, hits in matched.items()}
        details['title_match'] = title_match
        details['strong_match'] = strong_match
        return min(score, topic['cap']), details

    def _calculate_ai_infra_match(self, paper):
        """Compatibility wrapper: score the legacy ai_infra topic if configured."""
        topic = self._topics_by_id.get('ai_infra')
        if topic is None:
            return 0.0, {}
        return self._calculate_topic_match(paper, topic)

    @staticmethod
    def _term_in_text(term, text):
        """Match terms on alphanumeric boundaries to avoid AI/training-style collisions."""
        normalized = str(term).strip().lower()
        if not normalized:
            return False
        pattern = rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])"
        return re.search(pattern, str(text).lower()) is not None

    def _calculate_ai4sci_infra_match(self, paper):
        """Compatibility wrapper: score the legacy ai4sci_infra topic if configured."""
        topic = self._topics_by_id.get('ai4sci_infra')
        if topic is None:
            return 0.0, {}
        return self._calculate_topic_match(paper, topic)

    def _calculate_hpc_systems_match(self, paper):
        """Compatibility wrapper: score the legacy hpc_systems topic if configured."""
        topic = self._topics_by_id.get('hpc_systems')
        if topic is None:
            return 0.0, {}
        return self._calculate_topic_match(paper, topic)
    
    def _calculate_research_relevance(self, paper):
        """计算与研究方向的相关度"""
        profile = self.config.get('research_profile', {})
        primary = profile.get('primary_interests', [])
        secondary = profile.get('secondary_interests', [])
        
        text = (paper['title'] + ' ' + paper['abstract']).lower()
        
        score = 0.0
        for interest in primary:
            if interest.lower() in text:
                score += 3.0
        
        for interest in secondary:
            if interest.lower() in text:
                score += 1.5
        
        return min(score, 5.0)
    
    def generate_report(self, papers):
        """生成增强版Markdown报告"""
        date_str = datetime.now().strftime("%Y-%m-%d")
        
        # 尝试加载扩展摘要
        expanded_intros = {}
        papers_expanded = self._artifact_path('papers_expanded.json')
        if os.path.exists(papers_expanded):
            try:
                with open(papers_expanded, 'r', encoding='utf-8') as f:
                    expanded_data = json.load(f)
                    for p in expanded_data:
                        expanded_intros[p['url']] = p.get('detailed_intro', '')
                print("已加载扩展摘要")
            except Exception as e:
                print(f"加载扩展摘要失败: {e}")
        
        ordered_topics = [self._topics_by_id[tid] for tid in self._topic_order]
        topic_counts = {
            topic['id']: sum(
                1 for paper in papers if self._is_topic_paper(paper, topic['id'])
            )
            for topic in ordered_topics
        }
        topic_labels = [topic['label'] for topic in ordered_topics]

        def priority_arrow(labels):
            parts = []
            for index, label in enumerate(labels):
                if index == 0:
                    parts.append(f"{label}（首要）")
                elif index == 1:
                    parts.append(f"{label}（次级）")
                else:
                    parts.append(label)
            return ' → '.join(parts)

        title_topics = ' / '.join(topic_labels) if topic_labels else 'Custom Topics'
        research_line = priority_arrow(topic_labels) if topic_labels else '未配置主题'
        overview_topic_rows = ''.join(
            f"| {topic['label']} 命中 | {topic_counts[topic['id']]} |\n"
            for topic in ordered_topics
        )

        report = f"""# Daily arXiv Report — {title_topics} - {date_str}

> **🎓 研究方向**: {research_line}
> **🔧 监控领域**: {', '.join(self.config['categories'])}  
> **📊 论文总数**: {len(self.papers)}  
> **⭐ 智能推荐**: {len(papers)}  
> **生成时间**: {datetime.now().strftime("%Y-%m-%d %H:%M")}

---

## 📊 今日概览

| 指标 | 数值 |
|-----|------|
| 总论文数 | {len(self.papers)} |
| 推荐论文 | {len(papers)} |
{overview_topic_rows}| 平均评分 | {sum(p['relevance_score'] for p in papers) / len(papers):.1f}/10 |
| 有代码的论文 | {sum(1 for p in papers if p.get('code_url'))} |
| 包含性能指标 | {sum(1 for p in papers if p.get('metrics'))} |

---

## ⭐ 推荐论文

"""
        
        medals = ["🥇", "🥈", "🥉"]
        
        for i, paper in enumerate(papers, 1):
            medal = medals[i-1] if i <= 3 else f"**{i}.**"
            
            authors_str = ", ".join(paper['authors'][:5])
            if len(paper['authors']) > 5:
                authors_str += f", et al. ({len(paper['authors'])} authors)"
            
            arxiv_id = self._paper_arxiv_id(paper)
            
            # 评分详情
            score_details = paper.get('score_details', {})
            
            report += f"""
### {medal} {paper['title']}

**📊 相关度**: {paper['relevance_score']:.1f}/10"""
            
            # 显示评分细节
            if score_details:
                report += " | 评分构成: "
                parts = []
                if score_details.get('keywords', {}).get('score', 0) > 0:
                    parts.append(f"关键词{score_details['keywords']['score']:.1f}")
                for topic in self.topics:
                    topic_score = score_details.get(topic['id'], {}).get('score', 0)
                    if topic_score > 0:
                        parts.append(f"{topic['label']}{topic_score:.1f}")
                if score_details.get('authors', {}).get('score', 0) > 0:
                    parts.append(f"作者{score_details['authors']['score']:.1f}")
                if score_details.get('research_relevance', {}).get('score', 0) > 0:
                    parts.append(f"研究相关{score_details['research_relevance']['score']:.1f}")
                report += " + ".join(parts)
            
            primary_topic = paper.get('primary_topic', self._paper_primary_topic(paper))
            report += f"""  
**✍️ 作者**: {authors_str}  
**🔗 arXiv**: [{arxiv_id}]({paper['url']})  
**📅 提交**: {paper['submitted_date'].strftime("%Y-%m-%d")}  
**🏷️ 分类**: {', '.join(paper['categories'][:3])}
**🎯 首要主题**: {self.topic_labels.get(primary_topic, primary_topic)}
"""
            
            # 代码链接
            if paper.get('code_url'):
                report += f"**💻 代码**: {paper['code_url']}  \n"
            
            # 性能指标
            if paper.get('metrics'):
                metrics = paper['metrics']
                report += "**📈 性能指标**: "
                metric_strs = []
                if 'speedup' in metrics:
                    metric_strs.append(f"加速{metrics['speedup']}")
                if 'throughput' in metrics:
                    metric_strs.append(f"吞吐{metrics['throughput']}")
                if 'latency' in metrics:
                    metric_strs.append(f"延迟{metrics['latency']}")
                if 'memory' in metrics:
                    metric_strs.append(f"内存{metrics['memory']}")
                report += ", ".join(metric_strs) + "  \n"
            
            # 匹配的关键词
            if score_details.get('keywords', {}).get('matched'):
                matched = score_details['keywords']['matched'][:3]
                report += f"**🔑 匹配关键词**: {', '.join(matched)}  \n"

            for topic in self.topics:
                topic_details = score_details.get(topic['id'], {})
                group_hits = [
                    topic_details.get(group['name']) or []
                    for group in topic['term_groups']
                ]
                if not any(group_hits):
                    continue
                rendered_hits = ' × '.join(
                    ', '.join(hits[:3]) for hits in group_hits if hits
                )
                report += f"**🏷️ {topic['label']}**: {rendered_hits}  \n"
            
            # 使用扩展摘要或格式化的原始摘要
            intro = expanded_intros.get(paper['url'], '') or self._format_abstract(paper['abstract'])
            
            report += f"""
**📄 摘要**:

{intro}

---

"""
        
        report += f"""
## 💡 研究建议

基于你的研究方向,关注以下趋势:
- 检查有代码实现的论文,可能便于复现和对比
- 关注性能指标突出的工作(speedup > 2x)
- 留意同领域作者的follow-up工作

## 🔍 进一步分析

想深入了解某篇论文?可以 @我:
- "详细解释论文X的系统架构和实现细节"
- "这篇论文的方法能否应用到我的XX研究?"
- "帮我找论文X的相关工作和引用链"
- "对比论文X和Y的技术路线和性能"

---

**⏰ 下次更新**: {(datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")}  
**🎯 监控范围与顺序**: {research_line}
"""
        
        return report
    
    def _format_abstract(self, abstract):
        """格式化摘要"""
        sentences = re.split(r'(?<=[.!?])\s+', abstract)
        
        intro_parts = []
        current_para = []
        current_length = 0
        
        for sentence in sentences:
            current_para.append(sentence)
            current_length += len(sentence)
            
            if current_length > 150:
                intro_parts.append(' '.join(current_para))
                current_para = []
                current_length = 0
        
        if current_para:
            intro_parts.append(' '.join(current_para))
        
        return '\n\n'.join(intro_parts)
    
    def save_report(self, report, filename=None):
        """保存报告"""
        if filename is None:
            filename = f"arxiv_daily_{datetime.now().strftime('%Y%m%d')}.md"

        resolved_filename = self._artifact_path(filename)
        atomic_write(resolved_filename, report)
        
        print(f"报告已保存到: {resolved_filename}")
        return resolved_filename


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Daily Paper Brief monitor for arXiv AI/HPC systems papers")
    parser.add_argument(
        '--redo-today',
        action='store_true',
        help='ignore only IDs already pushed today, preserving historical deduplication',
    )
    return parser.parse_args(argv)


def main(argv=None):
    """主函数"""
    os.umask(0o077)
    args = parse_args(argv)
    print("=" * 50)
    print("Daily Paper Brief 文献监控 - 开始执行")
    print("=" * 50)

    config_path = resolve_config_path("arxiv-monitor-config-phd.json")
    base_dir = os.path.dirname(os.path.abspath(config_path))
    lock_path = os.path.join(base_dir, ".arxiv_monitor_phd.lock")
    lock = SingleRunLock(lock_path)

    if not lock.acquire():
        print("\n检测到已有 arXiv 监控实例正在运行，等待当前任务完成...")
        status, report_path = wait_for_existing_run(base_dir, lock_path)
        if status == "report":
            print(f"  已检测到现有报告: {report_path}")
            print("  本次不再重复执行。")
            return 0
        if status == "released":
            print("  现有实例已结束，本次不再重复执行。")
            return 0

        print("  等待现有实例完成超时，请检查当前运行中的任务。")
        return 1

    try:
        monitor = ArxivMonitorPhD(config_path, redo_today=args.redo_today)
        if args.redo_today:
            print("  重做模式: 仅忽略今天已推送的 ID，历史去重保持启用")
        monitor._archive_today_report()
        monitor._save_selected_papers([])

        print("\n[1/4] 正在抓取论文(最近3天,含去重)...")
        papers = monitor.fetch_papers(days=3)
        print(f"  共抓取 {len(papers)} 篇论文")

        if len(papers) == 0:
            print("SKIP_NO_NEW_PAPERS")
            print("\n今日无论文,任务结束。")
            return 0

        print("\n[2/4] 正在筛选论文...")
        filtered_papers = monitor.filter_papers()
        print(f"  筛选出 {len(filtered_papers)} 篇推荐论文")

        if len(filtered_papers) == 0:
            print("SKIP_NO_NEW_PAPERS")
            print("\n今日无推荐论文,任务结束。")
            return 0

        print("\n[3/4] 正在生成报告...")
        report = monitor.generate_report(filtered_papers)

        print("\n[4/4] 正在保存...")
        filename = monitor.save_report(report)

        # 只有真正完成保存的这一趟运行才更新已推送记录。
        monitor._mark_as_pushed(filtered_papers)
        print(f"  已标记 {len(filtered_papers)} 篇论文为已推送")

        print("\n" + "=" * 50)
        print("任务完成！")
        print("=" * 50)
        print(f"\n生成的报告: {filename}")
        print(f"待扩展论文: {monitor._artifact_path('papers_to_expand.json')}")
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
