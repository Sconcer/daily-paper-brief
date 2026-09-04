/* Daily Paper Brief 项目网页 demos —— 全部为合成数据，离线运行，不访问网络 */

/* ============ Demo A：选题评分排序器（arxiv_monitor_phd.py 的简化复刻） ============ */

const DEMO_A = (() => {
  const KEYWORDS = ["张量并行", "流水线并行", "通信压缩", "显存优化", "容错", "并行训练"];
  const AI_TERMS = ["大模型", "llm", "transformer", "预训练", "推理"];
  const SYS_TERMS = ["并行", "分布式", "显存", "调度", "集群", "allreduce", "编译", "通信"];
  const HPC_ANCHORS = ["超算", "hpc", "高性能计算", "slurm", "集群"];
  const HPC_MECH = ["集合通信", "i/o", "网络拓扑", "mpi", "通信", "调度"];
  const AI4SCI_DOMAINS = ["分子动力学", "气候", "蛋白质", "湍流", "第一性原理"];
  const AI4SCI_INFRA = ["gpu", "集群", "高性能计算", "加速", "超算"];
  const PRIORITY_AUTHORS = ["林一帆", "赵其"];
  const INSTITUTIONS = ["示例大学", "demo lab"];
  const PRIMARY_INTERESTS = ["并行训练", "通信"];
  const SECONDARY_INTERESTS = ["容错", "推理"];

  const DEFAULT_WEIGHTS = {
    keyword_match: 0.20, ai_infra_match: 0.35, ai4sci_match: 0.10, hpc_match: 0.20,
    author_match: 0.10, institution_match: 0.05, recency: 0.05, research_relevance: 0.15,
  };
  const WEIGHT_LABELS = {
    keyword_match: "关键词", ai_infra_match: "AI Infra", ai4sci_match: "AI4Sci",
    hpc_match: "HPC", author_match: "作者", institution_match: "机构",
    recency: "时效", research_relevance: "研究相关性",
  };
  const SUB_MAX = { keyword_match: 12, ai_infra_match: 8, ai4sci_match: 8, hpc_match: 8, author_match: 5, institution_match: 3, recency: 1, research_relevance: 5 };

  // 合成论文：不对应任何真实 arXiv 论文
  const PAPERS = [
    {
      id: "SYNTH-001",
      title: "面向千卡集群大模型预训练的流水线并行通信压缩框架（合成示例）",
      abstract: "本文提出一种分布式并行训练方法，在大模型预训练中结合流水线并行与 allreduce 通信压缩，降低集群通信开销并优化显存占用。",
      authors: "林一帆, 周四", inst: "示例大学", hours: 20,
    },
    {
      id: "SYNTH-002",
      title: "超算集群上分子动力学模拟的 I/O 与集合通信协同优化（合成示例）",
      abstract: "针对高性能计算集群上的分子动力学模拟，研究集合通信与 i/o 管线的协同调度，在超算系统上验证扩展性。",
      authors: "王可, 赵其", inst: "Demo Lab", hours: 30,
    },
    {
      id: "SYNTH-003",
      title: "面向蛋白质结构预测大模型推理的显存优化编译器（合成示例）",
      abstract: "为蛋白质结构预测大模型的推理阶段设计编译器级显存优化，在 gpu 集群上加速推理并降低峰值显存。",
      authors: "陈默", inst: "某研究所", hours: 80,
    },
    {
      id: "SYNTH-004",
      title: "一种通用的深度学习超参数调优工具（合成示例）",
      abstract: "介绍一个通用的深度学习超参数搜索工具，支持常见训练任务，不针对特定系统或科学领域。",
      authors: "孙明", inst: "某公司", hours: 50,
    },
    {
      id: "SYNTH-005",
      title: "HPC 作业调度中的容错机制综述（合成示例）",
      abstract: "综述 hpc 与 slurm 环境下作业调度的容错机制，覆盖检查点、迁移与网络拓扑感知调度。",
      authors: "赵其, 钱芳", inst: "Demo Lab", hours: 100,
    },
    {
      id: "SYNTH-006",
      title: "基于 Transformer 的湍流大涡模拟加速：GPU 集群上的分布式推理（合成示例）",
      abstract: "将 transformer 代理模型用于湍流大涡模拟加速，在 gpu 集群上实现分布式推理，并与高性能计算求解器耦合。",
      authors: "林一帆, 吴双", inst: "示例大学", hours: 12,
    },
  ];

  const countHits = (text, terms) => terms.filter((t) => text.includes(t.toLowerCase()));
  const cap8 = (v) => Math.min(v, 8);

  function scorePaper(p, w) {
    const title = p.title.toLowerCase();
    const abs = p.abstract.toLowerCase();
    const all = title + " " + abs;

    const kwT = countHits(title, KEYWORDS).length;
    const kwA = countHits(abs, KEYWORDS).length;
    const kw = kwT * 3 + kwA * 2;

    const ai = countHits(all, AI_TERMS);
    const sys = countHits(all, SYS_TERMS);
    const aiT = countHits(title, AI_TERMS).length > 0;
    const sysT = countHits(title, SYS_TERMS).length > 0;
    let aiInfra = 0;
    if (ai.length > 0 && sys.length > 0) {
      const strong = ai.length > 0 && sysT; // strong_match：AI 词命中且标题含系统词
      if (strong) aiInfra = cap8(4 + Math.min(ai.length, 3) * 0.75 + Math.min(sys.length, 5) * 0.6 + (aiT && sysT ? 1 : 0));
    }

    const anc = countHits(all, HPC_ANCHORS);
    const mech = countHits(all, HPC_MECH);
    const ancT = countHits(title, HPC_ANCHORS).length > 0;
    const mechT = countHits(title, HPC_MECH).length > 0;
    let hpc = 0;
    if (anc.length > 0 && mech.length > 0 && (ancT || mechT)) {
      hpc = cap8(3.5 + Math.min(anc.length, 3) * 0.9 + Math.min(mech.length, 5) * 0.65 + (ancT && mechT ? 0.75 : 0));
    }

    const dom = countHits(all, AI4SCI_DOMAINS);
    const inf = countHits(all, AI4SCI_INFRA);
    let ai4sci = 0;
    if (dom.length > 0 && inf.length > 0) {
      ai4sci = cap8(3 + Math.min(dom.length, 3) * 1.0 + Math.min(inf.length, 4) * 0.75);
    }

    const auth = PRIORITY_AUTHORS.filter((a) => p.authors.includes(a)).length * 2.5;
    const inst = INSTITUTIONS.filter((i) => p.inst.toLowerCase().includes(i)).length * 1.5;
    const rec = p.hours < 24 ? 1 : p.hours < 72 ? 0.5 : 0;
    const prim = countHits(all, PRIMARY_INTERESTS).length * 3;
    const sec = countHits(all, SECONDARY_INTERESTS).length * 1.5;
    const rel = Math.min(prim + sec, 5);

    const subs = {
      keyword_match: kw, ai_infra_match: aiInfra, ai4sci_match: ai4sci, hpc_match: hpc,
      author_match: auth, institution_match: inst, recency: rec, research_relevance: rel,
    };
    let total = 0;
    for (const k of Object.keys(subs)) total += subs[k] * w[k];
    total = Math.min(total, 10);

    let topic = "other";
    if (aiInfra > 0) topic = "ai_infra";
    else if (hpc > 0) topic = "hpc_systems";
    else if (ai4sci > 0) topic = "ai4sci_infra";
    return { total, subs, topic };
  }

  const TOPIC_LABELS = { ai_infra: ["AI Infra", "b-infra"], hpc_systems: ["HPC Systems", "b-hpc"], ai4sci_infra: ["AI4Sci", "b-ai4sci"], other: ["其他", "b-other"] };

  function render() {
    const weights = {};
    document.querySelectorAll("[data-weight]").forEach((el) => {
      weights[el.dataset.weight] = parseFloat(el.value);
      document.getElementById("wv-" + el.dataset.weight).textContent = parseFloat(el.value).toFixed(2);
    });
    const threshold = parseFloat(document.getElementById("demo-a-threshold").value);
    document.getElementById("demo-a-threshold-val").textContent = threshold.toFixed(1);

    const rows = PAPERS.map((p) => ({ paper: p, result: scorePaper(p, weights) }));
    rows.sort((a, b) => b.result.total - a.result.total);

    const container = document.getElementById("demo-a-results");
    container.innerHTML = "";
    let accepted = 0;
    rows.forEach(({ paper, result }, idx) => {
      const pass = result.total >= threshold;
      if (pass) accepted += 1;
      const div = document.createElement("div");
      div.className = "paper-row" + (pass ? "" : " rejected");
      const [topicLabel, topicClass] = pass ? TOPIC_LABELS[result.topic] : ["未过门槛", "b-reject"];
      const bars = Object.keys(result.subs).map((k) => {
        const raw = result.subs[k];
        const pct = Math.min(100, (raw / SUB_MAX[k]) * 100);
        return `<div class="sub-bar">${WEIGHT_LABELS[k]} ${raw.toFixed(2)}
          <div class="track"><div class="fill" style="width:${pct}%"></div></div></div>`;
      }).join("");
      div.innerHTML = `
        <div class="pr-top">
          <span class="pr-title">${idx + 1}. ${paper.title}</span>
          <span class="pr-badge ${topicClass}">${topicLabel}</span>
          <span class="pr-score">${result.total.toFixed(2)}</span>
        </div>
        <div class="pr-bars">${bars}</div>`;
      container.appendChild(div);
    });
    document.getElementById("demo-a-summary").textContent =
      `${accepted}/${PAPERS.length} 篇越过门槛（score ≥ ${threshold.toFixed(1)}），按总分降序排列；实际流水线还会做每日去重与主题配额（HPC ≤ 4 · AI4Sci ≤ 3 · 每日 ≤ 18）。`;
  }

  function init() {
    const panel = document.getElementById("demo-a-weights");
    if (!panel) return;
    Object.keys(DEFAULT_WEIGHTS).forEach((k) => {
      const wrap = document.createElement("div");
      wrap.innerHTML = `<label>${WEIGHT_LABELS[k]} <span class="val" id="wv-${k}">${DEFAULT_WEIGHTS[k].toFixed(2)}</span></label>
        <input type="range" min="0" max="0.5" step="0.01" value="${DEFAULT_WEIGHTS[k]}" data-weight="${k}">`;
      panel.appendChild(wrap);
    });
    document.querySelectorAll("[data-weight]").forEach((el) => el.addEventListener("input", render));
    document.getElementById("demo-a-threshold").addEventListener("input", render);
    render();
  }

  return { init };
})();

/* ============ Demo C：AI-writing-signals-v1.0 记分卡 ============ */

const DEMO_C = (() => {
  const METRICS = [
    { id: "chatbot_residue", name: "对话界面残留", family: "direct_artifact", hint: "如残留“作为 AI 助手……”等直接痕迹" },
    { id: "lexical_overrepresentation", name: "特征词过度集中", family: "lexical_style", hint: "特定高频词密度显著异常（单个 buzzword 不得记 2）" },
    { id: "formulaic_scaffolding", name: "模板化衔接", family: "discourse_style", hint: "段落开头/过渡句式模板化" },
    { id: "template_repetition", name: "句式与结构重复", family: "discourse_style", hint: "相邻句段结构高度同构" },
    { id: "rhythm_uniformity", name: "句段节奏同质化", family: "stylometry", hint: "句长/段长方差异常低" },
    { id: "terminology_notation_drift", name: "术语与符号漂移", family: "cross_section_consistency", hint: "同一概念跨章节术语或符号不一致" },
    { id: "citation_context_anomaly", name: "引文语境异常", family: "evidence_integrity", hint: "引文与上下文主张不匹配" },
    { id: "claim_evidence_miscalibration", name: "主张—证据失配", family: "evidence_integrity", hint: "结论强度超出所给证据" },
  ];
  const scores = new Array(METRICS.length).fill(0);

  function compute() {
    const total = scores.reduce((a, b) => a + b, 0);
    const disclosed = document.getElementById("demo-c-disclosed").checked;
    const coverage = document.getElementById("demo-c-coverage").checked;
    const positives = scores.filter((s) => s > 0).length;
    const families = new Set(METRICS.filter((m, i) => scores[i] > 0).map((m) => m.family)).size;
    const sections = Math.min(positives, 5); // 演示简化：阳性指标所在章节数按阳性指标数估计

    let label, cls, rule;
    if (disclosed) {
      label = "disclosed（已披露）"; cls = "lp-disclosed";
      rule = "存在使用披露时，分级固定为“已披露”，不再按分数分档。";
    } else if (!coverage) {
      label = "insufficient_evidence（证据不足）"; cls = "lp-insufficient";
      rule = "覆盖门未达标（自动分析词数、摘要/引言审阅、内容区覆盖、披露检索任一不足）时，禁止给出高置信度，只能报“证据不足”。";
    } else if (total <= 3) {
      label = "low（低）"; cls = "lp-low";
      rule = "序数总分 0–3 → 低。";
    } else if (total <= 7) {
      label = "medium（中）"; cls = "lp-medium";
      rule = "序数总分 4–7 → 中。";
    } else {
      const high = positives >= 3 && families >= 3 && sections >= 3;
      label = high ? "high（高）" : "medium（中，封顶）"; cls = high ? "lp-high" : "lp-medium";
      rule = high
        ? "总分 8–16 且 ≥3 个阳性指标、≥3 个指标族、≥3 个章节 → 高。"
        : `总分 8–16 但证据集中度不足（阳性指标 ${positives}、指标族 ${families}、章节 ${sections}）→ 封顶为中。`;
    }
    const confidence = coverage ? "可达 High" : "不得超过 Medium（覆盖不足）";

    document.getElementById("demo-c-total").innerHTML = `${total}<small> / 16</small>`;
    const pill = document.getElementById("demo-c-label");
    pill.textContent = label;
    pill.className = "label-pill " + cls;
    document.getElementById("demo-c-confidence").textContent = "置信度：" + confidence;
    document.getElementById("demo-c-rule").textContent = rule;
  }

  function init() {
    const container = document.getElementById("demo-c-metrics");
    if (!container) return;
    METRICS.forEach((m, i) => {
      const row = document.createElement("div");
      row.className = "metric-row";
      row.innerHTML = `
        <div>
          <div class="m-name">${m.name} <span class="m-fam">${m.id} · ${m.family}</span></div>
          <div class="m-hint">${m.hint}</div>
        </div>
        <div class="seg" data-metric="${i}">
          <button type="button" data-v="0" class="on-0">0</button><button type="button" data-v="1">1</button><button type="button" data-v="2">2</button>
        </div>`;
      container.appendChild(row);
    });
    container.addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      const seg = btn.parentElement;
      const idx = parseInt(seg.dataset.metric, 10);
      scores[idx] = parseInt(btn.dataset.v, 10);
      seg.querySelectorAll("button").forEach((b) => (b.className = ""));
      btn.className = "on-" + btn.dataset.v;
      compute();
    });
    document.getElementById("demo-c-disclosed").addEventListener("change", compute);
    document.getElementById("demo-c-coverage").addEventListener("change", compute);
    compute();
  }

  return { init };
})();

document.addEventListener("DOMContentLoaded", () => {
  DEMO_A.init();
  DEMO_C.init();
});
