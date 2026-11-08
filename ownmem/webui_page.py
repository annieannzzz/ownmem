# -*- coding: utf-8 -*-
"""OwnMem 审计台页面（纯字符串模块，无逻辑）。

设计语言：**一本翻开的台账**。
- 纸面底 + 墨色字，唯一的彩色是「章」——朱红，只出现在当前项、关键数字、拦截上；
- 不画卡片：区块之间靠发丝线分隔，像印刷品的分栏，而不是 SaaS 仪表盘；
- 数据一律等宽字体、等宽数字（tabular-nums），小数点对齐才有可能被比较；
- 账本用单字标记：结 / 过 / 坑；重要度用 常 / 关 / 高 / 普 / 低，不用彩色药丸；
- 审计流水 90% 以上是召回注入，默认只看「有动作」的事件，纯读的噪音收进日历条。

页面回答三个问题：
  1. 我的 agent 到底记了什么？（记忆仓库）
  2. 谁在什么时候动了记忆？（审计流水）
  3. 有没有背着我存东西？（待定区 + 拦截记录）
"""

PAGE = r"""<!doctype html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OwnMem 审计台</title>
<meta name="color-scheme" content="dark light">
<link rel="icon" href="data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2032%2032'%3E%3Crect%20width='32'%20height='32'%20fill='%23a63a26'/%3E%3Ctext%20x='16'%20y='23'%20font-size='20'%20font-family='SimSun,serif'%20text-anchor='middle'%20fill='%23fdf7f0'%3E%E8%AE%B0%3C/text%3E%3C/svg%3E">
<style>
  *,*::before,*::after { box-sizing:border-box; }
  html { -webkit-text-size-adjust:100%; }
  html,body { min-height:100%; }
  :root {
    /* 墨（默认深色） */
    --paper:#0f0f0d; --paper-2:#15150f; --paper-3:#1b1b16;
    --rule:#26261f; --rule-2:#3a3a30;
    --ink:#ece8dc; --ink-2:#a6a196; --ink-3:#767065;
    --seal:#d1513c; --seal-ink:#170c08; --seal-wash:rgba(209,81,60,.12);
    --moss:#7bb08a; --ochre:#c9a25a; --indigo:#8aa8c2;
    --wash:rgba(255,248,232,.036); --sel:rgba(255,248,232,.062);
    --sans:"Inter","Segoe UI Variable Text","Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei UI","Noto Sans SC",system-ui,sans-serif;
    --mono:"JetBrains Mono","Berkeley Mono","Cascadia Mono","SFMono-Regular",ui-monospace,Consolas,"Liberation Mono",monospace;
    --serif:"Source Serif 4",Georgia,"Songti SC",SimSun,serif;
    --shadow:0 28px 60px -40px rgba(0,0,0,.9);
    color-scheme:dark;
  }
  [data-theme="light"] {
    /* 纸 */
    --paper:#f6f4ee; --paper-2:#fffdf7; --paper-3:#edeae1;
    --rule:#e1ddd0; --rule-2:#c6c1b1;
    --ink:#1a1813; --ink-2:#55514a; --ink-3:#8b8679;
    --seal:#a63a26; --seal-ink:#fdf7f0; --seal-wash:rgba(166,58,38,.09);
    --moss:#3a6b4e; --ochre:#8a6420; --indigo:#36556f;
    --wash:rgba(26,24,19,.032); --sel:rgba(26,24,19,.055);
    --shadow:0 28px 60px -44px rgba(60,52,34,.55);
    color-scheme:light;
  }
  body {
    margin:0; background:var(--paper); color:var(--ink);
    font:13.5px/1.6 var(--sans);
    -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
  }
  ::selection { background:var(--seal-wash); }
  a { color:inherit; }
  button { font-family:inherit; }
  .mono { font-family:var(--mono); font-variant-numeric:tabular-nums; }
  .num { font-variant-numeric:tabular-nums; }
  .muted { color:var(--ink-3); }
  .sr { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap; }

  /* ============ 骨架 ============ */
  .rail {
    position:fixed; inset:0 auto 0 0; width:238px; z-index:40;
    display:flex; flex-direction:column;
    background:var(--paper); border-right:1px solid var(--rule);
  }
  .brand { display:flex; align-items:center; gap:11px; padding:22px 20px 20px; }
  .seal-mark {
    width:30px; height:30px; flex:0 0 auto; display:grid; place-items:center;
    background:var(--seal); color:var(--seal-ink); border-radius:2px;
    font:400 16px/1 var(--serif); padding-bottom:1px;
  }
  .wm { display:flex; flex-direction:column; gap:4px; }
  .wm b { font:650 13px/1 var(--sans); letter-spacing:.16em; text-transform:uppercase; }
  .wm span { font:10px/1 var(--mono); letter-spacing:.1em; color:var(--ink-3); }
  .nav { display:flex; flex-direction:column; gap:1px; padding:2px 10px; flex:1; overflow-y:auto; }
  .nav-cap { font:10px/1 var(--mono); letter-spacing:.18em; color:var(--ink-3); padding:16px 10px 9px; }
  .nav-item {
    display:grid; grid-template-columns:17px 1fr auto; align-items:center; gap:9px;
    height:31px; padding:0 10px; border:0; border-radius:3px; background:none;
    color:var(--ink-2); font:13px/1 var(--sans); text-align:left; cursor:pointer;
    position:relative; transition:background .12s, color .12s;
  }
  .nav-item .idx { font:10px/1 var(--mono); color:var(--ink-3); letter-spacing:.04em; }
  .nav-item .cnt { font:10px/1 var(--mono); color:var(--ink-3); font-variant-numeric:tabular-nums; }
  .nav-item .cnt.hot { color:var(--seal); }
  .nav-item:hover { background:var(--wash); color:var(--ink); }
  .nav-item[aria-current="page"] { background:var(--sel); color:var(--ink); font-weight:600; }
  .nav-item[aria-current="page"] .idx { color:var(--seal); }
  .nav-item[aria-current="page"]::before {
    content:""; position:absolute; left:-10px; top:7px; bottom:7px; width:2px; background:var(--seal);
  }
  .rail-foot { padding:14px 20px 18px; border-top:1px solid var(--rule); display:flex; flex-direction:column; gap:9px; }
  .live { display:flex; align-items:center; gap:7px; font:10.5px/1 var(--mono); color:var(--ink-2); letter-spacing:.04em; }
  .live i { width:5px; height:5px; background:var(--moss); animation:breathe 3.6s ease-in-out infinite; }
  @keyframes breathe { 0%,100% { opacity:1; } 50% { opacity:.35; } }
  .rail-foot .path { font:10px/1.5 var(--mono); color:var(--ink-3); word-break:break-all; }
  .rail-foot .keys { display:flex; flex-wrap:wrap; gap:4px 8px; font:10px/1.4 var(--mono); color:var(--ink-3); }
  kbd {
    font:10px/1 var(--mono); color:var(--ink-2); background:var(--paper-3);
    border:1px solid var(--rule); border-bottom-color:var(--rule-2);
    border-radius:2px; padding:2px 4px; margin-right:3px;
  }

  .main { margin-left:238px; min-width:0; }
  .view { display:none; }
  .view.active { display:block; }
  .viewhead {
    display:flex; align-items:flex-end; gap:24px; padding:26px 40px 20px;
    border-bottom:1px solid var(--rule);
  }
  .vh-l { min-width:0; }
  .eyebrow { font:10px/1 var(--mono); letter-spacing:.2em; color:var(--ink-3); text-transform:uppercase; }
  .viewhead h1 { margin:11px 0 0; font:600 25px/1.15 var(--sans); letter-spacing:-.01em; }
  .viewhead p { margin:8px 0 0; font-size:13px; color:var(--ink-3); }
  .vh-r { margin-left:auto; display:flex; align-items:center; gap:6px; flex:0 0 auto; }
  .body { padding:0 40px 110px; max-width:1180px; }

  /* ============ 控件 ============ */
  .btn {
    display:inline-flex; align-items:center; justify-content:center; gap:6px;
    height:29px; padding:0 11px; border:1px solid var(--rule-2); border-radius:3px;
    background:transparent; color:var(--ink); font:12.5px/1 var(--sans);
    cursor:pointer; white-space:nowrap; text-decoration:none;
    transition:background .12s, border-color .12s, color .12s;
  }
  .btn:hover { background:var(--wash); border-color:var(--ink-3); }
  .btn:disabled { opacity:.45; cursor:default; }
  .btn-sm { height:25px; padding:0 8px; font-size:11.5px; }
  .btn-icon { width:29px; padding:0; }
  .btn-icon.btn-sm { width:25px; }
  .btn-primary { background:var(--ink); border-color:var(--ink); color:var(--paper); font-weight:600; }
  .btn-primary:hover { background:var(--ink-2); border-color:var(--ink-2); }
  .btn-quiet { border-color:transparent; color:var(--ink-2); }
  .btn-quiet:hover { border-color:transparent; background:var(--wash); color:var(--ink); }
  .btn-danger { border-color:transparent; color:var(--seal); }
  .btn-danger:hover { border-color:transparent; background:var(--seal-wash); }
  :where(.btn,.nav-item,.mode,.seg-b,.tg,.input,.sel):focus-visible {
    outline:1px solid var(--seal); outline-offset:2px;
  }

  .input {
    height:29px; padding:0 9px; border:1px solid var(--rule-2); border-radius:3px;
    background:var(--paper); color:var(--ink); font:12.5px/1 var(--sans); outline:none; min-width:0;
    transition:border-color .12s;
  }
  .input::placeholder { color:var(--ink-3); }
  .input:focus { border-color:var(--ink-3); }
  .sel { appearance:none; -webkit-appearance:none; padding-right:24px; cursor:pointer; }
  .sel-w { position:relative; display:inline-flex; align-items:center; }
  .sel-w::after {
    content:""; position:absolute; right:8px; width:5px; height:5px; pointer-events:none;
    border-right:1px solid var(--ink-3); border-bottom:1px solid var(--ink-3);
    transform:translateY(-2px) rotate(45deg);
  }
  .search { position:relative; display:inline-flex; align-items:center; }
  .search .input { padding-left:27px; }
  .search svg { position:absolute; left:8px; color:var(--ink-3); pointer-events:none; }

  .seg { display:inline-flex; border:1px solid var(--rule-2); border-radius:3px; overflow:hidden; }
  .seg-b {
    border:0; border-right:1px solid var(--rule); background:transparent; color:var(--ink-3);
    height:27px; padding:0 10px; font:12px/1 var(--sans); cursor:pointer; transition:background .12s, color .12s;
  }
  .seg-b:last-child { border-right:0; }
  .seg-b:hover { color:var(--ink); background:var(--wash); }
  .seg-b[aria-pressed="true"] { background:var(--sel); color:var(--ink); font-weight:600; }

  .tg {
    display:inline-flex; align-items:center; gap:7px; height:29px; padding:0 9px;
    border:1px solid var(--rule-2); border-radius:3px; background:transparent;
    color:var(--ink-2); font:12px/1 var(--sans); cursor:pointer; transition:border-color .12s, color .12s;
  }
  .tg:hover { color:var(--ink); }
  .tg i { width:9px; height:9px; border:1px solid var(--ink-3); border-radius:1px; position:relative; }
  .tg[aria-pressed="true"] { color:var(--ink); border-color:var(--ink-3); }
  .tg[aria-pressed="true"] i { background:var(--seal); border-color:var(--seal); }

  .bar {
    position:sticky; top:0; z-index:20; display:flex; flex-wrap:wrap; align-items:center; gap:7px;
    padding:13px 0; background:var(--paper); border-bottom:1px solid var(--rule);
  }
  .bar .note { font:11px/1.5 var(--mono); color:var(--ink-3); letter-spacing:.02em; }
  .sp { margin-left:auto; }

  /* ============ 区块 ============ */
  .sec { padding:26px 0; border-bottom:1px solid var(--rule); }
  .sec:last-child { border-bottom:0; }
  .sec-h { display:flex; align-items:baseline; gap:12px; margin-bottom:16px; flex-wrap:wrap; }
  .sec-h .eyebrow { flex:0 0 auto; }
  .sec-meta { font:10.5px/1 var(--mono); color:var(--ink-3); letter-spacing:.04em; }

  /* 总览：陈述句 + 数字 */
  .statement { padding:30px 0 4px; }
  .stmt-asof { font:10px/1 var(--mono); letter-spacing:.16em; color:var(--ink-3); margin-bottom:16px; }
  .stmt {
    margin:0; max-width:820px; font:400 19px/1.8 var(--sans); letter-spacing:.004em; color:var(--ink);
  }
  .stmt em { font:500 19px/1 var(--mono); font-style:normal; letter-spacing:-.01em; padding:0 1px; }
  .stmt em.hot { color:var(--seal); }
  .stmt .dim { color:var(--ink-3); }
  .figs {
    display:grid; grid-template-columns:repeat(4,1fr); margin:28px 0 0;
    border-top:1px solid var(--rule);
  }
  .fig { padding:16px 18px 2px 0; border-right:1px solid var(--rule); }
  .fig:last-child { border-right:0; }
  .fig .n { font:400 33px/1 var(--mono); letter-spacing:-.03em; }
  .fig .n.hot { color:var(--seal); }
  .fig .k { margin-top:9px; font:11.5px/1 var(--sans); color:var(--ink-2); letter-spacing:.03em; }
  .fig .d { margin-top:6px; font:10px/1.4 var(--mono); color:var(--ink-3); }

  .meter { position:relative; height:2px; background:var(--rule); margin:22px 0 0; }
  .meter i { position:absolute; left:0; top:0; bottom:0; background:var(--ink); }
  .meter b { position:absolute; top:-3px; width:1px; height:8px; background:var(--rule-2); }
  .meter-cap { display:flex; justify-content:space-between; margin-top:8px;
    font:10px/1 var(--mono); color:var(--ink-3); letter-spacing:.06em; }

  /* 结构条 */
  .cols2 { display:grid; grid-template-columns:1fr 1fr; gap:0 40px; }
  .col-cap { font:10px/1 var(--mono); letter-spacing:.16em; color:var(--ink-3); padding-bottom:9px; }
  .srow { display:grid; grid-template-columns:58px 1fr 42px; gap:12px; align-items:center;
    padding:7px 0; border-bottom:1px solid var(--rule); }
  .srow .lbl { font:11.5px/1 var(--sans); color:var(--ink-2); }
  .srow .track { height:3px; background:var(--rule); position:relative; }
  .srow .track i { position:absolute; left:0; top:0; bottom:0; background:var(--ink-2); }
  .srow .n { font:11.5px/1 var(--mono); text-align:right; color:var(--ink); }

  /* 动静条 */
  .act { display:grid; grid-template-columns:repeat(14,1fr); gap:1px; height:54px;
    background:var(--rule); border:1px solid var(--rule); }
  .act .cell { background:var(--paper); position:relative; display:flex; align-items:flex-end; }
  .act .cell .fill { width:100%; background:var(--ink-3); opacity:.5; min-height:0; }
  .act .cell.has-block .fill { opacity:.62; }
  .act .cell .blk { position:absolute; left:0; right:0; bottom:0; height:2px; background:var(--seal); }
  .act-days { display:grid; grid-template-columns:repeat(14,1fr); gap:1px; margin-top:6px;
    font:9.5px/1 var(--mono); color:var(--ink-3); text-align:center; }
  .act-days span.today { color:var(--ink); }

  /* 模式 */
  .modes { display:grid; grid-template-columns:repeat(4,1fr); border:1px solid var(--rule); }
  .mode {
    position:relative; text-align:left; padding:13px 14px 14px; border:0;
    border-right:1px solid var(--rule); background:transparent; color:var(--ink); cursor:pointer;
    font-family:inherit; transition:background .13s;
  }
  .mode:last-child { border-right:0; }
  .mode:hover { background:var(--wash); }
  .mode[aria-pressed="true"] { background:var(--sel); }
  .mode[aria-pressed="true"]::before {
    content:""; position:absolute; left:-1px; right:-1px; top:-1px; height:2px; background:var(--seal);
  }
  .m-top { display:flex; align-items:center; gap:8px; }
  .m-box { width:10px; height:10px; border:1px solid var(--rule-2); flex:0 0 auto;
    display:grid; place-items:center; }
  .mode[aria-pressed="true"] .m-box { border-color:var(--seal); }
  .mode[aria-pressed="true"] .m-box::after { content:""; width:5px; height:5px; background:var(--seal); }
  .m-name { font:600 12.5px/1 var(--sans); }
  .m-desc { margin-top:9px; font:11.5px/1.6 var(--sans); color:var(--ink-3); min-height:2.9em; }
  .m-rw { display:flex; gap:9px; margin-top:10px; font:10px/1 var(--mono); letter-spacing:.06em; color:var(--ink-2); }
  .m-rw .off { color:var(--ink-3); text-decoration:line-through; }

  /* ============ 记忆行 ============ */
  .rows { border-top:1px solid var(--rule); }
  .row {
    display:grid; grid-template-columns:20px minmax(0,1fr) 108px; gap:14px;
    padding:13px 4px 12px; border-bottom:1px solid var(--rule); cursor:pointer;
    transition:background .1s;
  }
  .row:hover { background:var(--wash); }
  .mark {
    width:19px; height:19px; display:grid; place-items:center; border:1px solid var(--rule-2);
    border-radius:1px; font:500 11px/1 var(--sans); color:var(--ink-2); margin-top:1px;
  }
  .mark.k-conclusion { color:var(--ink); }
  .mark.k-process { color:var(--indigo); border-color:var(--indigo); }
  .mark.k-pitfall { color:var(--seal); border-color:var(--seal); }
  .row-t {
    font:13.5px/1.68 var(--sans); color:var(--ink); word-break:break-word;
    display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden;
  }
  .row-t.oneline { -webkit-line-clamp:1; }
  .row-m { display:flex; flex-wrap:wrap; align-items:center; gap:5px 12px; margin-top:8px;
    font:10.5px/1 var(--mono); color:var(--ink-3); letter-spacing:.02em; }
  .row-m .sep { color:var(--rule-2); }
  .row-m .tag { color:var(--ink-2); }
  .row-m .void { color:var(--ochre); }
  .row-r { text-align:right; display:flex; flex-direction:column; align-items:flex-end; gap:8px; padding-top:2px; }
  .imp { font:10px/1 var(--mono); letter-spacing:.12em; color:var(--ink-3); }
  .imp.always { color:var(--seal); }
  .imp.critical { color:var(--ochre); }
  .imp.high { color:var(--ink-2); }
  .row-d { font:10px/1 var(--mono); color:var(--ink-3); }

  .more { display:flex; align-items:center; justify-content:center; gap:12px; padding:22px 0 4px; }
  .more .n { font:10px/1 var(--mono); color:var(--ink-3); letter-spacing:.06em; }

  /* ============ 审计流水 ============ */
  .tl-day { display:flex; align-items:center; gap:12px; padding:20px 0 9px;
    font:10px/1 var(--mono); letter-spacing:.16em; color:var(--ink-3); }
  .tl-day::after { content:""; flex:1; height:1px; background:var(--rule); }
  .ev {
    display:grid; grid-template-columns:46px 14px minmax(0,1fr); gap:12px; align-items:start;
    padding:9px 4px; border-bottom:1px solid var(--rule); cursor:pointer;
    transition:background .1s;
  }
  .ev:hover { background:var(--wash); }
  .ev-t { font:10.5px/1.65 var(--mono); color:var(--ink-3); padding-top:1px; text-align:right; }
  .ev-i { padding-top:2px; color:var(--ink-3); display:flex; }
  .ev-i.read { color:var(--indigo); }
  .ev-i.write { color:var(--moss); }
  .ev-i.block { color:var(--seal); }
  .ev-i.warn { color:var(--ochre); }
  .ev-h { display:flex; align-items:baseline; gap:9px; flex-wrap:wrap; }
  .ev-n { font:600 12.5px/1.4 var(--sans); }
  .ev-a { font:10.5px/1 var(--mono); color:var(--ink-3); }
  .ev-x { font:10px/1 var(--mono); color:var(--seal); letter-spacing:.04em; }
  .ev-d {
    margin-top:3px; font:12.5px/1.62 var(--sans); color:var(--ink-2); word-break:break-word;
    display:-webkit-box; -webkit-line-clamp:1; -webkit-box-orient:vertical; overflow:hidden;
  }
  .ev.quiet .ev-n { font-weight:500; color:var(--ink-2); }

  /* ============ 表格 ============ */
  table { width:100%; border-collapse:collapse; }
  thead th {
    text-align:left; padding:0 12px 9px 0; border-bottom:1px solid var(--rule-2);
    font:10px/1 var(--mono); letter-spacing:.14em; color:var(--ink-3); text-transform:uppercase;
    font-weight:500; white-space:nowrap;
  }
  tbody td { padding:11px 12px 11px 0; border-bottom:1px solid var(--rule); font-size:12.5px; vertical-align:top; }
  tbody tr:hover { background:var(--wash); }
  th.num, td.num { text-align:right; font-family:var(--mono); font-variant-numeric:tabular-nums; }
  th:last-child, td:last-child { padding-right:0; }
  .cell2 { display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden;
    line-height:1.6; color:var(--ink-2); }
  .acts { display:flex; gap:6px; justify-content:flex-end; }
  .sid { display:inline-flex; align-items:center; gap:5px; font:11px/1 var(--mono); color:var(--ink-2); cursor:pointer; }
  .sid:hover { color:var(--ink); }

  /* ============ 待定区 ============ */
  .grp { margin-bottom:26px; }
  .grp-h { display:flex; align-items:center; gap:10px; padding-bottom:9px; border-bottom:1px solid var(--rule-2);
    font:10.5px/1 var(--mono); color:var(--ink-3); letter-spacing:.06em; }
  .grp-h b { color:var(--ink-2); font-weight:500; }
  .st {
    display:grid; grid-template-columns:16px minmax(0,1fr); gap:12px; padding:13px 4px 12px;
    border-bottom:1px solid var(--rule); cursor:pointer;
  }
  .st:hover { background:var(--wash); }
  .st input { margin:4px 0 0; accent-color:var(--seal); width:13px; height:13px; cursor:pointer; }
  .st-c { font:13px/1.68 var(--sans); color:var(--ink); white-space:pre-wrap; word-break:break-word; }
  .st-m { margin-top:7px; font:10px/1 var(--mono); color:var(--ink-3); letter-spacing:.04em; }
  .bulk {
    position:sticky; bottom:0; z-index:18; display:flex; align-items:center; gap:10px;
    padding:13px 0; background:var(--paper); border-top:1px solid var(--rule-2);
  }
  .bulk .cnt { font:11px/1 var(--mono); color:var(--ink-2); letter-spacing:.04em; }

  /* ============ 空 / 载入 ============ */
  .empty { padding:52px 0; text-align:center; border-bottom:1px solid var(--rule); }
  .empty .e-t { font:12.5px/1 var(--sans); color:var(--ink-2); }
  .empty .e-s { margin-top:9px; font:10.5px/1.6 var(--mono); color:var(--ink-3); letter-spacing:.04em; }
  .loading-bar { position:fixed; left:0; right:0; top:0; height:1px; z-index:60; overflow:hidden; display:none; }
  .loading-bar.on { display:block; }
  .loading-bar i { position:absolute; top:0; bottom:0; width:30%; background:var(--seal);
    animation:sweep 1.1s cubic-bezier(.4,0,.2,1) infinite; }
  @keyframes sweep { from { left:-30%; } to { left:100%; } }

  /* ============ 抽屉 ============ */
  .scrim { position:fixed; inset:0; z-index:50; background:transparent; pointer-events:none; }
  .scrim.on { background:rgba(0,0,0,.22); pointer-events:auto; }
  [data-theme="light"] .scrim.on { background:rgba(40,36,26,.14); }
  .drawer {
    position:fixed; top:0; right:0; bottom:0; width:min(540px,100%); z-index:55;
    display:flex; flex-direction:column; background:var(--paper-2);
    border-left:1px solid var(--rule-2); box-shadow:var(--shadow);
    animation:slidein .18s cubic-bezier(.2,.8,.3,1) both;
  }
  @keyframes slidein { from { transform:translateX(14px); opacity:0; } to { transform:none; opacity:1; } }
  .dr-h { display:flex; align-items:center; gap:10px; padding:16px 20px; border-bottom:1px solid var(--rule); }
  .dr-h .t { font:600 12.5px/1 var(--sans); display:flex; align-items:center; gap:8px; }
  .dr-b { flex:1; overflow-y:auto; padding:20px; }
  .dr-f { display:flex; align-items:center; gap:8px; padding:13px 20px; border-top:1px solid var(--rule);
    background:var(--paper); }
  .dr-text { font:14px/1.8 var(--sans); color:var(--ink); white-space:pre-wrap; word-break:break-word; }
  .kv { display:grid; grid-template-columns:62px minmax(0,1fr); gap:9px 16px; margin:22px 0 0;
    padding-top:18px; border-top:1px solid var(--rule); font-size:12px; }
  .kv dt { font:10px/1.9 var(--mono); letter-spacing:.12em; color:var(--ink-3); text-transform:uppercase; }
  .kv dd { margin:0; color:var(--ink-2); font-family:var(--mono); font-size:11px; line-height:1.9; word-break:break-all; }
  .chain { display:flex; flex-direction:column; gap:8px; margin-top:10px; }
  .chain-l {
    text-align:left; border:1px solid var(--rule); border-radius:2px; background:var(--paper);
    padding:9px 11px; cursor:pointer; font:11.5px/1.6 var(--sans); color:var(--ink-2);
  }
  .chain-l:hover { border-color:var(--ink-3); color:var(--ink); }
  .chain-l b { display:block; font:10px/1 var(--mono); color:var(--ink-3); letter-spacing:.12em; margin-bottom:5px; font-weight:500; }

  /* ============ 询问框 ============ */
  .ask-wrap { position:fixed; inset:0; z-index:80; display:grid; place-items:center; padding:24px;
    background:rgba(0,0,0,.42); animation:fadein .12s ease both; }
  [data-theme="light"] .ask-wrap { background:rgba(40,36,26,.22); }
  @keyframes fadein { from { opacity:0; } to { opacity:1; } }
  .ask { width:min(400px,100%); background:var(--paper-2); border:1px solid var(--rule-2); border-radius:3px;
    box-shadow:var(--shadow); padding:20px 22px 18px; }
  .ask h3 { margin:0; font:600 13.5px/1.4 var(--sans); }
  .ask p { margin:9px 0 0; font:12.5px/1.7 var(--sans); color:var(--ink-2); word-break:break-word; }
  .ask-f { display:flex; gap:8px; justify-content:flex-end; margin-top:20px; }
  .ask-f .ok-danger { background:var(--seal); border-color:var(--seal); color:var(--seal-ink); font-weight:600; }
  .ask-f .ok-danger:hover { background:var(--seal); border-color:var(--seal); opacity:.88; }

  /* ============ 提示 ============ */
  .toasts { position:fixed; right:24px; bottom:22px; z-index:90; display:flex; flex-direction:column;
    gap:7px; max-width:340px; }
  .toast {
    display:flex; align-items:flex-start; gap:9px; padding:10px 13px; background:var(--paper-2);
    border:1px solid var(--rule-2); border-left:2px solid var(--moss); border-radius:2px;
    font:12.5px/1.55 var(--sans); color:var(--ink); box-shadow:var(--shadow);
    animation:toastin .18s cubic-bezier(.2,.8,.3,1) both;
  }
  .toast.err { border-left-color:var(--seal); }
  .toast.out { animation:toastout .16s ease forwards; }
  @keyframes toastin { from { opacity:0; transform:translateY(6px); } to { opacity:1; transform:none; } }
  @keyframes toastout { to { opacity:0; transform:translateY(4px); } }

  .stopped { display:grid; place-items:center; min-height:100vh; text-align:center; }
  .stopped h2 { font:600 16px/1.4 var(--sans); margin:0 0 8px; }
  .stopped p { font:11px/1.6 var(--mono); color:var(--ink-3); margin:0; letter-spacing:.06em; }

  /* ============ 响应式 ============ */
  @media (max-width:1080px) {
    .figs { grid-template-columns:repeat(2,1fr); }
    .fig { border-right:0; border-bottom:1px solid var(--rule); padding-bottom:14px; }
    .fig:nth-child(odd) { border-right:1px solid var(--rule); }
    .cols2 { grid-template-columns:1fr; gap:26px; }
    .modes { grid-template-columns:repeat(2,1fr); }
    .mode:nth-child(2) { border-right:0; }
    .mode:nth-child(-n+2) { border-bottom:1px solid var(--rule); }
  }
  @media (max-width:860px) {
    .rail { position:static; width:auto; flex-direction:row; align-items:center;
      border-right:0; border-bottom:1px solid var(--rule); padding:8px 12px; gap:8px; }
    .brand { padding:4px 6px; }
    .wm span { display:none; }
    .nav { flex-direction:row; padding:0; gap:2px; overflow-x:auto; }
    .nav-cap, .rail-foot, .nav-item .idx, .nav-item .cnt { display:none; }
    .nav-item { height:29px; }
    .nav-item[aria-current="page"]::before { left:0; right:0; top:auto; bottom:-9px; width:auto; height:2px; }
    .main { margin-left:0; }
    .viewhead { padding:18px 18px 16px; flex-wrap:wrap; }
    .viewhead h1 { font-size:20px; }
    .vh-r { margin-left:0; }
    .body { padding:0 18px 80px; }
    .row { grid-template-columns:20px minmax(0,1fr); }
    .row-r { grid-column:2; flex-direction:row; align-items:center; gap:10px; padding-top:6px; }
    .act, .act-days { grid-template-columns:repeat(7,1fr); }
    .act .cell:nth-child(n+8), .act-days span:nth-child(n+8) { display:none; }
  }
  @media (prefers-reduced-motion:reduce) {
    *,*::before,*::after { animation-duration:.01ms !important; animation-iteration-count:1 !important;
      transition-duration:.01ms !important; }
  }
</style>
<script>
try {
  var s = localStorage.getItem('ownmem-theme');
  var p = new URLSearchParams(location.search).get('theme');
  var t = (p === 'light' || p === 'dark') ? p : (s || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'));
  document.documentElement.dataset.theme = t;
} catch (e) {}
</script>
</head>
<body>
<div class="loading-bar" id="loading"><i></i></div>

<aside class="rail">
  <div class="brand">
    <span class="seal-mark" aria-hidden="true">记</span>
    <span class="wm"><b>OwnMem</b><span>记忆台账 · 本地</span></span>
  </div>
  <nav class="nav" id="nav" aria-label="视图"></nav>
  <div class="rail-foot" id="side-foot">
    <div class="live"><i></i>本地服务 · v__VERSION__</div>
    <div class="path"></div>
    <div class="keys"><span><kbd>/</kbd>搜索</span><span><kbd>1</kbd>–<kbd>6</kbd>切换</span></div>
  </div>
</aside>

<div class="main">
  <header class="viewhead">
    <div class="vh-l">
      <div class="eyebrow" id="view-eyebrow">01 / 总览</div>
      <h1 id="view-title">总览</h1>
      <p id="view-sub">记忆资产与运行状态</p>
    </div>
    <div class="vh-r">
      <a class="btn btn-quiet" href="/api/export" download title="把整本账导出成 JSON">
        <span data-icon="download" data-size="13"></span><span>导出</span>
      </a>
      <button class="btn btn-icon btn-quiet" id="theme-btn" title="切换纸色" aria-label="切换主题"></button>
      <button class="btn btn-icon btn-quiet" id="refresh-btn" title="刷新" aria-label="刷新"></button>
      <button class="btn btn-danger btn-sm" id="stop-btn" title="停止审计台服务" aria-label="停止服务">
        <span data-icon="power" data-size="12"></span>
      </button>
    </div>
  </header>

  <main class="body">
    <section class="view" id="view-overview">
      <div class="statement" id="hero"></div>

      <section class="sec">
        <div class="sec-h"><span class="eyebrow">STRUCTURE</span><span class="sec-meta" id="struct-meta"></span></div>
        <div class="cols2" id="struct"></div>
      </section>

      <section class="sec">
        <div class="sec-h"><span class="eyebrow">ACTIVITY · 14 天</span><span class="sec-meta" id="activity-meta"></span></div>
        <div class="act" id="activity"></div>
        <div class="act-days" id="activity-days"></div>
      </section>

      <section class="sec">
        <div class="sec-h"><span class="eyebrow">MODE</span><span class="sec-meta">默认模式 · 对所有新会话生效</span></div>
        <div class="modes" id="mode-grid"></div>
      </section>

      <section class="sec">
        <div class="sec-h"><span class="eyebrow">LATEST</span><span class="sec-meta">最近的动静 · 只列有动作的事件</span>
          <span class="sp"></span>
          <button class="btn btn-sm btn-quiet" data-action="nav" data-view="audit">整本流水 →</button>
        </div>
        <div id="recent-audit"></div>
      </section>
    </section>

    <section class="view" id="view-memories">
      <div class="bar">
        <div class="search">
          <span data-icon="search" data-size="13"></span>
          <input class="input" id="mem-q" placeholder="搜索记忆内容…" style="width:250px" autocomplete="off" aria-label="搜索记忆">
        </div>
        <div class="seg" id="mem-kind" role="group" aria-label="账本筛选"></div>
        <div class="sel-w">
          <select class="input sel" id="mem-imp" aria-label="重要度筛选">
            <option value="">全部重要度</option>
            <option value="always">常驻</option>
            <option value="critical">关键</option>
            <option value="high">高</option>
            <option value="normal">普通</option>
            <option value="low">低</option>
          </select>
        </div>
        <div class="sel-w"><select class="input sel" id="mem-agent" aria-label="来源筛选"></select></div>
        <span class="sp"></span>
        <span class="note" id="mem-count"></span>
        <button class="btn btn-sm btn-quiet" data-action="mem-reset">清空</button>
      </div>
      <div class="rows" id="mem-list"></div>
    </section>

    <section class="view" id="view-audit">
      <div class="bar">
        <button class="tg" id="audit-key" data-action="audit-key" aria-pressed="true"><i></i>只看有动作的</button>
        <div class="sel-w">
          <select class="input sel" id="audit-event" aria-label="事件筛选"></select>
        </div>
        <input class="input" id="audit-session" placeholder="会话 ID" style="width:150px" autocomplete="off" aria-label="会话筛选">
        <span class="sp"></span>
        <span class="note" id="audit-count"></span>
        <button class="tg" id="audit-live" data-action="audit-live" aria-pressed="false"><i></i>实时</button>
        <button class="btn btn-sm btn-quiet" data-action="audit-refresh">刷新</button>
      </div>
      <div id="audit-list"></div>
    </section>

    <section class="view" id="view-staged">
      <div class="bar">
        <span class="note">隐身模式下被拦下的内容 · 只有你点头才进长期记忆</span>
        <span class="sp"></span>
        <button class="btn btn-sm btn-quiet" data-action="staged-all">全选</button>
      </div>
      <div id="staged-list"></div>
      <div class="bulk" id="staged-bulk" style="display:none">
        <span class="cnt" id="staged-cnt"></span>
        <span class="sp"></span>
        <button class="btn" data-action="staged" data-kind="discard">丢弃</button>
        <button class="btn btn-primary" data-action="staged" data-kind="promote">保留进长期记忆</button>
      </div>
    </section>

    <section class="view" id="view-trash">
      <div class="bar">
        <span class="note">删除的记忆先落这里 · 「彻底删除」是物理抹除，不可恢复</span>
        <span class="sp"></span>
        <button class="btn btn-sm btn-quiet" data-action="trash-refresh">刷新</button>
      </div>
      <div id="trash-list"></div>
    </section>

    <section class="view" id="view-sessions">
      <div class="bar">
        <div class="search">
          <span data-icon="search" data-size="13"></span>
          <input class="input" id="sess-q" placeholder="按 agent / 项目 / 会话 ID 过滤" style="width:280px" autocomplete="off">
        </div>
        <span class="sp"></span>
        <span class="note" id="sess-count"></span>
        <button class="btn btn-sm btn-quiet" data-action="sess-refresh">刷新</button>
      </div>
      <div id="sess-wrap"></div>
    </section>
  </main>
</div>

<div class="scrim" id="scrim"></div>
<aside class="drawer" id="drawer" style="display:none" role="dialog" aria-modal="true" aria-labelledby="drawer-title">
  <div class="dr-h">
    <span class="t" id="drawer-title"></span>
    <span class="sp"></span>
    <button class="btn btn-icon btn-sm btn-quiet" data-action="close" aria-label="关闭" data-icon="x" data-size="14"></button>
  </div>
  <div class="dr-b" id="drawer-body"></div>
  <div class="dr-f" id="drawer-foot"></div>
</aside>

<div class="toasts" id="toasts" aria-live="polite" aria-atomic="false"></div>

<script>
'use strict';

/* ---------------- 基础 ---------------- */
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]));

const ICONS = {
  search:'<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
  download:'<path d="M12 3v12"/><path d="m7 11 5 5 5-5"/><path d="M5 21h14"/>',
  power:'<path d="M18.4 6.6a9 9 0 1 1-12.8 0"/><path d="M12 2v9"/>',
  refresh:'<path d="M21 12a9 9 0 1 1-2.6-6.4L21 8"/><path d="M21 3v5h-5"/>',
  sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M6.3 17.7l-1.4 1.4M19.1 4.9l-1.4 1.4"/>',
  moon:'<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
  x:'<path d="M18 6 6 18M6 6l12 12"/>',
  check:'<path d="M20 6 9 17l-5-5"/>',
  dot:'<circle cx="12" cy="12" r="3.2" fill="currentColor" stroke="none"/>',
  read:'<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="2.6"/>',
  pen:'<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4z"/>',
  slash:'<circle cx="12" cy="12" r="9"/><path d="m5.7 18.3 12.6-12.6"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  inbox:'<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.5 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.7 1.1z"/>',
  trash:'<path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M10 11v6M14 11v6"/>',
  rotate:'<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
  alert:'<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
  sliders:'<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3"/><path d="M1 14h6M9 8h6M17 16h6"/>',
  flag:'<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><path d="M4 22v-7"/>',
  file:'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>',
  arrow:'<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>'
};
function icon(name, size) {
  const p = ICONS[name] || ICONS.dot;
  const n = size || 14;
  return '<svg width="' + n + '" height="' + n + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + p + '</svg>';
}
function hydrate() {
  $$('[data-icon]').forEach(el => {
    el.innerHTML = icon(el.dataset.icon, parseInt(el.dataset.size || '14', 10));
  });
}

/* ---------------- 文案 ---------------- */
const VIEWS = [
  { id:'overview', label:'总览',     idx:'01', sub:'记忆资产与运行状态', key:'' },
  { id:'memories', label:'记忆仓库', idx:'02', sub:'它到底记了什么',     key:'' },
  { id:'audit',    label:'审计流水', idx:'03', sub:'谁在什么时候动了记忆', key:'' },
  { id:'staged',   label:'待定区',   idx:'04', sub:'被拦下的内容，等你裁决', key:'' },
  { id:'trash',    label:'回收站',   idx:'05', sub:'删掉的还没真删',     key:'' },
  { id:'sessions', label:'会话',     idx:'06', sub:'每次会话的读写账单',  key:'' }
];
const KIND = {
  conclusion:{ ch:'结', label:'结论' },
  process:{ ch:'过', label:'过程' },
  pitfall:{ ch:'坑', label:'踩坑' }
};
const IMP = { always:'常驻', critical:'关键', high:'高', normal:'普通', low:'低' };
const VIEWS_CN = { normal:'全量', 'result-only':'只看结论', 'blind-review':'盲审', 'cross-check':'交叉验证' };
/* 事件 → [后端文案兜底, 语气]；文案以 /api/meta 为准 */
const EV = {
  recall:['召回注入','read'], recall_blocked:['召回拦截','block'], proxy_inject:['网关注入','read'],
  write:['写入记忆','write'], write_blocked:['写入拦截','block'], write_triage_reject:['闸门丢弃','block'],
  write_triage_stage:['闸门转待定','warn'], stage:['转入待定','warn'], promote:['挑选保存','write'],
  supersede:['结论取代','write'], recur:['踩坑复发','warn'], kind_promote:['账本晋级','write'],
  delete:['删除记忆','block'], restore:['恢复记忆','write'], purge:['彻底删除','block'],
  redacted:['审计脱敏','warn'], evict:['容量淘汰','warn'], wipe:['抹除来源','block'],
  mode:['模式切换',''], project:['项目切换',''], session_end:['会话结束',''],
  digest:['会话沉淀',''], digest_inject:['上次进度注入','read'],
  answer_first:['先答后对',''], view:['视角切换','']
};
const EV_ICON = {
  recall:'read', recall_blocked:'slash', proxy_inject:'read', write:'pen', write_blocked:'slash',
  write_triage_reject:'slash', write_triage_stage:'inbox', stage:'inbox', promote:'check',
  supersede:'rotate', recur:'alert', kind_promote:'arrow', delete:'trash', restore:'rotate',
  purge:'trash', redacted:'slash', evict:'trash', wipe:'alert', mode:'sliders', project:'flag',
  session_end:'flag', digest:'file', digest_inject:'clock', answer_first:'read', view:'read'
};
/* 纯读事件：默认折叠——审计流水 93% 是这类噪音，混在一起就没人看得见真正发生的事 */
const ROUTINE = ['recall', 'proxy_inject', 'digest_inject', 'view'];
const KEY_EVENTS = Object.keys(EV).filter(k => ROUTINE.indexOf(k) < 0);
const MODE_ICON = { normal:'read', incognito:'slash', blank:'file', ghost:'x' };

const state = {
  view: 'overview',
  overview: null,
  mem: { q:'', kind:'', imp:'', agent:'', offset:0, items:[], total:0, ranked:false },
  audit: { event:'', session:'', key:true, offset:0, items:[], total:0, live:false },
  staged: [], sessions: [], trash: [],
  cache: {}
};
let MODES = {};
let liveTimer = null;

/* ---------------- 工具 ---------------- */
function relTime(ts) {
  if (!ts) return '';
  const t = new Date(ts);
  if (isNaN(t.getTime())) return String(ts).slice(5, 16).replace('T', ' ');
  const d = (Date.now() - t.getTime()) / 1000;
  if (d < 45) return '刚刚';
  if (d < 3600) return Math.floor(d / 60) + ' 分钟前';
  if (d < 86400) return Math.floor(d / 3600) + ' 小时前';
  if (d < 172800) return '昨天';
  if (d < 86400 * 8) return Math.floor(d / 86400) + ' 天前';
  return String(ts).slice(5, 10).replace('-', '/');
}
function dayLabel(day) {
  const dt = new Date(day + 'T00:00:00');
  const w = '日一二三四五六'[dt.getDay()] || '';
  const today = new Date();
  const iso = today.getFullYear() + '-' + String(today.getMonth() + 1).padStart(2, '0') + '-' + String(today.getDate()).padStart(2, '0');
  const head = day === iso ? '今天' : day.slice(5).replace('-', '月') + '日';
  return head + ' · 周' + w;
}
function nf(n) { return String(n == null ? 0 : n).replace(/\B(?=(\d{3})+(?!\d))/g, ','); }
function shortSid(sid) { return sid ? '…' + String(sid).slice(-6) : '-'; }

async function api(path, opts) {
  const bar = $('#loading');
  if (bar) bar.classList.add('on');
  try {
    let r;
    try {
      r = await fetch(path, Object.assign({ headers: { 'X-OwnMem-UI':'1' } }, opts || {}));
    } catch (e) {
      throw new Error('连不上本地服务：' + e.message);
    }
    const text = await r.text();
    let d = {};
    try { d = JSON.parse(text); } catch (e) { d = {}; }
    if (!r.ok) throw new Error(d.error || d.message || ('请求失败 ' + r.status));
    return d;
  } finally {
    if (bar) bar.classList.remove('on');
  }
}
function toast(msg, kind) {
  const wrap = $('#toasts');
  const el = document.createElement('div');
  el.className = 'toast' + (kind === 'err' ? ' err' : '');
  el.innerHTML = '<span style="color:' + (kind === 'err' ? 'var(--seal)' : 'var(--moss)') +
    ';margin-top:1px">' + icon(kind === 'err' ? 'alert' : 'check', 13) + '</span><span>' + esc(msg) + '</span>';
  wrap.appendChild(el);
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 180); }, 3400);
}
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('已复制 ' + text);
  } catch (e) { toast('复制失败，手动选一下吧', 'err'); }
}
function empty(title, sub) {
  return '<div class="empty"><div class="e-t">' + esc(title) + '</div>' +
    (sub ? '<div class="e-s">' + esc(sub) + '</div>' : '') + '</div>';
}

/* ---------------- 路由 ---------------- */
function parseHash() {
  const raw = (location.hash || '').replace(/^#\/?/, '');
  const qi = raw.indexOf('?');
  const path = qi < 0 ? raw : raw.slice(0, qi);
  const query = new URLSearchParams(qi < 0 ? '' : raw.slice(qi + 1));
  const parts = path.split('/').filter(Boolean);
  return { parts, query };
}
function go(hash, replace) {
  if (replace) location.replace('#' + hash);
  else if (location.hash !== '#' + hash) location.hash = hash;
  else route();
}
function route() {
  const { parts, query } = parseHash();
  const head = parts[0] || 'overview';
  if (head === 'memory' && parts[1]) {
    // 深链：抽屉打开某条记忆；背后得有一层视图撑着，默认落在记忆仓库
    if (!$('.view.active')) showView('memories');
    openMemory(parts[1], true);
    return;
  }
  const view = VIEWS.some(v => v.id === head) ? head : 'overview';
  if (view === 'audit' && query.get('events') === 'all') state.audit.key = false;
  if (view === 'memories' && query.get('kind')) state.mem.kind = query.get('kind');
  closeDrawer(true);
  showView(view);
}
function renderNav() {
  const o = state.overview;
  const counts = {
    memories: o ? nf(o.stats.active) : '',
    audit: o ? nf(o.audit_total) : '',
    staged: o && o.staged_n ? String(o.staged_n) : '',
    trash: o && o.stats.trashed ? String(o.stats.trashed) : '',
    sessions: o ? nf(o.sessions_total) : '',
    overview: ''
  };
  $('#nav').innerHTML = '<div class="nav-cap">台账</div>' + VIEWS.map(v => {
    const c = counts[v.id] || '';
    const hot = (v.id === 'staged' && o && o.staged_n) ? ' hot' : '';
    return '<button class="nav-item" data-action="nav" data-view="' + v.id + '"' +
      (state.view === v.id ? ' aria-current="page"' : '') + '>' +
      '<span class="idx">' + v.idx + '</span><span>' + v.label + '</span>' +
      '<span class="cnt' + hot + '">' + c + '</span></button>';
  }).join('');
}
function showView(id) {
  state.view = id;
  $$('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + id));
  const v = VIEWS.find(x => x.id === id) || VIEWS[0];
  $('#view-eyebrow').textContent = v.idx + ' / ' + v.label;
  $('#view-title').textContent = v.label;
  $('#view-sub').textContent = v.sub;
  document.title = v.label + ' · OwnMem 审计台';
  $$('.nav-item').forEach(b => {
    if (b.dataset.view === id) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });
  stopLive();
  const run = {
    overview: loadOverview, memories: loadMemories, audit: loadAudit,
    staged: loadStaged, trash: loadTrash, sessions: loadSessions
  }[id];
  const p = run ? run() : null;
  if (p && p.catch) p.catch(e => toast(e.message, 'err'));
}

/* ---------------- 总览 ---------------- */
async function loadOverview() {
  const [o, st] = await Promise.all([
    api('/api/overview'),
    api('/api/staged').catch(() => ({ items: [] }))
  ]);
  o.staged_n = (st.items || []).length;
  state.overview = o;
  state.staged = st.items || [];
  renderNav();
  renderStatement(o);
  renderStructure(o);
  renderActivity(o);
  await loadModes();
  const au = await api('/api/audit?limit=9&events=' + encodeURIComponent(KEY_EVENTS.join(',')))
    .catch(() => ({ items: [] }));
  renderTimeline($('#recent-audit'), au.items, true);
  renderFoot(o);
}

function renderStatement(o) {
  const s = o.stats;
  const pct = Math.min(100, Math.round((s.active / Math.max(1, s.capacity)) * 100));
  const act = o.activity || [];
  const today = act[act.length - 1] || { n:0, w:0, b:0 };
  const always = (s.by_importance || {}).always || 0;
  const stagedN = o.staged_n || 0;
  const parts = [];
  parts.push('这台机器记着 <em>' + nf(s.active) + '</em> 条记忆，其中 <em class="hot">' + nf(always) +
    '</em> 条是每次会话都在场的常驻红线。');
  if (stagedN) {
    parts.push('<em class="hot">' + stagedN + '</em> 条被拦下的内容躺在待定区，等你点头或丢弃。');
  } else if (o.blocked_total) {
    parts.push('累计拦下 <em>' + nf(o.blocked_total) + '</em> 次写入，一次都没漏到长期记忆里。');
  } else {
    parts.push('至今没有写过一条没经过你的内容。');
  }
  parts.push('<span class="dim">今天发生 ' + nf(today.n) + ' 次读写，其中写入 ' + nf(today.w) +
    ' 次' + (today.b ? '、拦截 ' + today.b + ' 次' : '') + '。</span>');

  const asof = (o.now || '').slice(0, 16).replace('T', ' ');
  $('#hero').innerHTML =
    '<div class="stmt-asof">AS OF ' + esc(asof) + ' · 本地时间</div>' +
    '<p class="stmt">' + parts.join('') + '</p>' +
    '<div class="figs">' +
      fig(nf(s.active), '在册记忆', '容量 ' + nf(s.capacity) + ' · ' + pct + '%', '') +
      fig(nf(always), '常驻在场', '每次会话强制注入', 'hot') +
      fig(String(stagedN), '待定区', stagedN ? '等你裁决' : '干净的', stagedN ? 'hot' : '') +
      fig(nf(s.trashed), '回收站', '还能捞回来', '') +
    '</div>' +
    '<div class="meter">' +
      '<i style="width:' + pct + '%"></i>' +
      [10,20,30,40,50,60,70,80,90].map(p => '<b style="left:' + p + '%"></b>').join('') +
    '</div>' +
    '<div class="meter-cap"><span>0</span><span>' + pct + '% 的容量已用</span><span>' + nf(s.capacity) + '</span></div>';
}
function fig(n, k, d, cls) {
  return '<div class="fig"><div class="n' + (cls ? ' ' + cls : '') + '">' + n + '</div>' +
    '<div class="k">' + k + '</div><div class="d">' + d + '</div></div>';
}

function renderStructure(o) {
  const s = o.stats;
  const imps = [['always','常驻'], ['critical','关键'], ['high','高'], ['normal','普通'], ['low','低']];
  const kinds = [['conclusion','结论'], ['process','过程'], ['pitfall','踩坑']];
  const byImp = s.by_importance || {};
  const byKind = s.by_kind || {};
  const maxImp = Math.max(1, ...imps.map(([k]) => byImp[k] || 0));
  const maxKind = Math.max(1, ...kinds.map(([k]) => byKind[k] || 0));
  const rowBar = (label, n, max, cls) =>
    '<div class="srow"><span class="lbl">' + label + '</span>' +
    '<span class="track"><i style="width:' + Math.round((n / max) * 100) + '%;' + (cls ? 'background:' + cls : '') + '"></i></span>' +
    '<span class="n">' + nf(n) + '</span></div>';
  $('#struct').innerHTML =
    '<div><div class="col-cap">按重要度</div>' +
      imps.map(([k, l]) => rowBar(l, byImp[k] || 0, maxImp, k === 'always' ? 'var(--seal)' : '')).join('') +
    '</div>' +
    '<div><div class="col-cap">按账本</div>' +
      kinds.map(([k, l]) => rowBar(l, byKind[k] || 0, maxKind,
        k === 'pitfall' ? 'var(--seal)' : (k === 'process' ? 'var(--indigo)' : ''))).join('') +
    '</div>';
  const agents = Object.keys(s.by_agent || {});
  $('#struct-meta').textContent = '作废 ' + (s.superseded || 0) + ' · 复发坑 ' + (s.recurring_pitfalls || 0) +
    ' · 来源 ' + agents.length + ' 个 agent';
}

function renderActivity(o) {
  const act = o.activity || [];
  const max = Math.max(1, ...act.map(d => d.n));
  const sum = act.reduce((a, d) => a + d.n, 0);
  const w = act.reduce((a, d) => a + d.w, 0);
  const b = act.reduce((a, d) => a + d.b, 0);
  $('#activity').innerHTML = act.map(d =>
    '<div class="cell' + (d.b ? ' has-block' : '') + '" title="' + esc(d.day) + ' · ' + nf(d.n) +
    ' 次事件 · 写 ' + d.w + ' · 拦 ' + d.b + '">' +
    '<span class="fill" style="height:' + Math.max(d.n ? 3 : 0, Math.round((d.n / max) * 100)) + '%"></span>' +
    (d.b ? '<span class="blk"></span>' : '') + '</div>').join('');
  const today = act.length ? act[act.length - 1].day : '';
  $('#activity-days').innerHTML = act.map(d =>
    '<span class="' + (d.day === today ? 'today' : '') + '">' + d.day.slice(8) + '</span>').join('');
  $('#activity-meta').textContent = nf(sum) + ' 次事件 · 写入 ' + nf(w) + ' · 拦截 ' + b +
    ' · 竖条为当日强度，红脚为有拦截';
}

function renderFoot(o) {
  $('#side-foot').innerHTML =
    '<div class="live"><i></i>本地服务 · v' + esc(o.version) + '</div>' +
    '<div class="path" title="' + esc(o.stats.home) + '">' + esc(o.stats.home) + '</div>' +
    '<div class="keys"><span><kbd>/</kbd>搜索</span><span><kbd>1</kbd>–<kbd>6</kbd>切换</span></div>';
}

async function loadModes() {
  const d = await api('/api/modes');
  MODES = d.modes || {};
  const def = d.default_mode;
  $('#mode-grid').innerHTML = Object.entries(MODES).map(([k, m]) => {
    const on = k === def;
    return '<button class="mode" data-action="set-mode" data-mode="' + k + '" aria-pressed="' + on +
      '" title="' + esc(m.desc) + '">' +
      '<div class="m-top"><span class="m-box"></span><span class="m-name">' + esc(m.label) + '</span></div>' +
      '<div class="m-desc">' + esc(m.desc) + '</div>' +
      '<div class="m-rw"><span class="' + (m.read ? '' : 'off') + '">读</span>' +
      '<span class="' + (m.write ? '' : 'off') + '">写</span>' +
      (on ? '<span style="color:var(--seal);margin-left:auto">默认</span>' : '') +
      '</div></button>';
  }).join('');
}

/* ---------------- 时间线 ---------------- */
function groupEvents(items) {
  const out = [];
  (items || []).forEach(e => {
    const day = (e.ts || '').slice(0, 10);
    const g = out[out.length - 1];
    if (g && g.event === e.event && g.agent === (e.agent || '') && g.day === day) g.items.push(e);
    else out.push({ event: e.event, agent: e.agent || '', day: day, items: [e] });
  });
  return out;
}
function renderTimeline(box, items, compact) {
  items = items || [];
  if (!items.length) {
    box.innerHTML = empty('没有记录', compact ? '' : '换个筛选条件试试，或把「只看有动作的」关掉');
    return;
  }
  const groups = groupEvents(items);
  let html = '', lastDay = '';
  groups.forEach(g => {
    if (g.day !== lastDay) {
      html += '<div class="tl-day">' + esc(dayLabel(g.day)) + '</div>';
      lastDay = g.day;
    }
    const n = g.items.length;
    const first = g.items[0];
    const t0 = (first.ts || '').slice(11, 16);
    const t1 = (g.items[n - 1].ts || '').slice(11, 16);
    const tone = (EV[g.event] || ['', ''])[1] || '';
    const label = (EV[g.event] || [g.event])[0];
    const detail = first.detail || '';
    const idx = g.items.findIndex(x => x.detail);
    const shown = detail || (idx >= 0 ? g.items[idx].detail : '');
    html += '<div class="ev' + (tone ? '' : ' quiet') + '" data-action="open-ev"' +
      ' data-ev="' + esc(g.event) + '" data-gi="' + groups.indexOf(g) + '">' +
      '<span class="ev-t">' + esc(t0) + (n > 1 && t1 !== t0 ? '<br>' + esc(t1) : '') + '</span>' +
      '<span class="ev-i ' + tone + '">' + icon(EV_ICON[g.event] || 'dot', 13) + '</span>' +
      '<span>' +
        '<span class="ev-h"><span class="ev-n">' + esc(label) + '</span>' +
          (n > 1 ? '<span class="ev-x">×' + n + '</span>' : '') +
          '<span class="ev-a">' + esc(g.agent || '-') + '</span>' +
          (!compact && first.mode ? '<span class="ev-a">' + esc(first.mode) + '</span>' : '') +
        '</span>' +
        (shown && !compact ? '<span class="ev-d">' + esc(shown) + '</span>' : '') +
      '</span></div>';
  });
  box.innerHTML = html;
  box._groups = groups;
}

/* ---------------- 记忆仓库 ---------------- */
function renderKindSeg() {
  const opts = [['','全部'], ['conclusion','结论'], ['process','过程'], ['pitfall','踩坑']];
  $('#mem-kind').innerHTML = opts.map(([v, l]) =>
    '<button class="seg-b" data-action="mem-kind" data-kind="' + v + '" aria-pressed="' +
    (state.mem.kind === v) + '">' + l + '</button>').join('');
}
function fillAgentSelect() {
  const s = (state.overview && state.overview.stats) || {};
  const agents = Object.keys(s.by_agent || {});
  const sel = $('#mem-agent');
  const cur = state.mem.agent;
  sel.innerHTML = '<option value="">全部来源</option>' +
    agents.map(a => '<option value="' + esc(a) + '">' + esc(a) + '</option>').join('');
  sel.value = agents.indexOf(cur) >= 0 ? cur : '';
}
async function loadMemories(append) {
  const m = state.mem;
  if (!append) m.offset = 0;
  const p = new URLSearchParams({ limit: 60, offset: m.offset });
  if (m.q) p.set('q', m.q);
  if (m.agent) p.set('agent', m.agent);
  if (m.kind) p.set('kind', m.kind);
  if (m.imp) p.set('importance', m.imp);
  const box = $('#mem-list');
  if (!append) box.innerHTML = empty('读取中', '');
  try {
    if (!state.overview) await loadOverview().catch(() => {});
    fillAgentSelect();
    const d = await api('/api/memories?' + p.toString());
    const items = d.items || [];
    (d.items || []).forEach(x => { state.cache[x.id] = x; });
    m.total = d.total || 0;
    m.ranked = !!d.ranked;
    if (append) m.items = m.items.concat(items); else m.items = items;
    m.hasMore = !!d.has_more;
    m.offset = m.items.length;
    renderMemories();
  } catch (e) {
    box.innerHTML = empty('加载失败', e.message);
  }
}
function renderMemories() {
  const m = state.mem;
  const box = $('#mem-list');
  $('#mem-count').textContent = m.total ? ('显示 ' + m.items.length + ' / ' + nf(m.total) + ' 条' +
    (m.ranked ? '（按相关度）' : '')) : '';
  if (!m.items.length) {
    box.innerHTML = empty('没有匹配的记忆', '换个关键词，或者放宽筛选');
    return;
  }
  box.innerHTML = m.items.map(memRow).join('') +
    (m.hasMore ? '<div class="more"><button class="btn btn-sm" data-action="mem-more">载入更多</button>' +
      '<span class="n">还有 ' + nf(m.total - m.items.length) + ' 条</span></div>' : '');
}
function memRow(x) {
  const k = KIND[x.kind] || KIND.conclusion;
  const tags = (x.tags || []).slice(0, 3);
  const meta = [
    '<span>' + esc(x.agent || 'unknown') + '</span>',
    x.project ? '<span class="tag">' + esc(x.project) + '</span>' : '',
    '<span>命中 ' + (x.hits || 0) + '</span>',
    x.recurred ? '<span class="void">复发 ' + x.recurred + ' 次</span>' : '',
    x.superseded_by ? '<span class="void">已被取代</span>' : '',
    tags.map(t => '<span class="tag">#' + esc(t) + '</span>').join('')
  ].filter(Boolean).join('<span class="sep">·</span>');
  return '<article class="row" data-action="open-mem" data-id="' + esc(x.id) + '" tabindex="0">' +
    '<span class="mark k-' + esc(x.kind || 'conclusion') + '" title="' + k.label + '">' + k.ch + '</span>' +
    '<div><div class="row-t">' + esc(x.content) + '</div>' +
    '<div class="row-m">' + meta + '</div></div>' +
    '<div class="row-r"><span class="imp ' + esc(x.importance) + '">' + (IMP[x.importance] || x.importance) + '</span>' +
    '<span class="row-d">' + esc((x.created_at || '').slice(0, 10)) + '</span></div></article>';
}

/* ---------------- 审计流水 ---------------- */
function renderEventOptions() {
  const opts = [['','全部事件']].concat(Object.keys(EV).map(k => [k, (EV[k] || [])[0] || k]));
  $('#audit-event').innerHTML = opts.map(([v, l]) =>
    '<option value="' + v + '">' + l + '</option>').join('');
}
async function loadAudit(append) {
  const a = state.audit;
  if (!append) a.offset = 0;
  const p = new URLSearchParams({ limit: 120, offset: a.offset });
  if (a.event) p.set('events', a.event);
  else if (a.key) p.set('events', KEY_EVENTS.join(','));
  if (a.session) p.set('session_id', a.session);
  const box = $('#audit-list');
  if (!append) box.innerHTML = empty('读取中', '');
  try {
    const d = await api('/api/audit?' + p.toString());
    const items = d.items || [];
    a.total = d.total || 0;
    if (append) a.items = a.items.concat(items); else a.items = items;
    a.hasMore = !!d.has_more;
    a.offset = a.items.length;
    $('#audit-count').textContent = nf(a.total) + ' 条' + (a.key && !a.event ? '（已滤掉纯读事件）' : '');
    renderAudit();
  } catch (e) {
    box.innerHTML = empty('加载失败', e.message);
  }
}
function renderAudit() {
  const a = state.audit;
  const box = $('#audit-list');
  if (!a.items.length) {
    box.innerHTML = empty('没有记录', a.key ? '这段时间没有任何写入、拦截或改动' : '换个筛选条件');
    return;
  }
  renderTimeline(box, a.items, false);
  if (a.hasMore) {
    box.innerHTML += '<div class="more"><button class="btn btn-sm" data-action="audit-more">载入更多</button>' +
      '<span class="n">还有 ' + nf(a.total - a.items.length) + ' 条</span></div>';
  }
}
function startLive() {
  stopLive();
  liveTimer = setInterval(() => {
    if (state.view !== 'audit' || !state.audit.live) return;
    if (document.querySelector('.drawer').style.display !== 'none') return;
    loadAudit().catch(() => {});
  }, 5000);
}
function stopLive() { if (liveTimer) { clearInterval(liveTimer); liveTimer = null; } }

/* ---------------- 待定区 ---------------- */
async function loadStaged() {
  const box = $('#staged-list');
  try {
    const d = await api('/api/staged');
    state.staged = d.items || [];
    if (!state.staged.length) {
      box.innerHTML = empty('待定区是空的', '隐身模式下被拦下的内容才会落在这里');
      $('#staged-bulk').style.display = 'none';
      return;
    }
    const bySess = {};
    state.staged.forEach(it => { (bySess[it.session_id] = bySess[it.session_id] || []).push(it); });
    box.innerHTML = Object.entries(bySess).map(([sid, list]) =>
      '<div class="grp"><div class="grp-h"><b>' + esc(shortSid(sid)) + '</b>' +
        '<span>会话</span><span class="sp"></span><span>' + list.length + ' 条待定</span></div>' +
      list.map(it =>
        '<label class="st"><input type="checkbox" class="js-st" data-session="' + esc(sid) +
          '" data-sid="' + esc(it.sid) + '">' +
        '<span><span class="st-c">' + esc(it.content) + '</span>' +
        '<span class="st-m">' + esc((it.staged_at || '').slice(0, 16).replace('T', ' ')) +
          (it.agent ? ' · ' + esc(it.agent) : '') + '</span></span></label>').join('') +
      '</div>').join('');
    syncBulk();
  } catch (e) { box.innerHTML = empty('加载失败', e.message); }
}
function syncBulk() {
  const n = $$('.js-st:checked').length;
  const bar = $('#staged-bulk');
  bar.style.display = n ? 'flex' : 'none';
  $('#staged-cnt').textContent = '已选 ' + n + ' 条';
}
async function stagedAction(kind) {
  const boxes = $$('.js-st:checked');
  if (!boxes.length) { toast('先勾选要处理的条目', 'err'); return; }
  const verb = kind === 'promote' ? '写进长期记忆' : '丢弃';
  const ok = await ask(verb + '这 ' + boxes.length + ' 条？',
    kind === 'promote' ? '写进长期记忆后，它们会像普通记忆一样被召回。' : '丢弃后就不在待定区了，审计流水里仍留着这次拦截的记录。',
    verb, kind === 'discard');
  if (!ok) return;
  const by = {};
  boxes.forEach(b => { (by[b.dataset.session] = by[b.dataset.session] || []).push(b.dataset.sid); });
  try {
    for (const [sid, ids] of Object.entries(by)) {
      const r = await api('/api/' + kind + '?session=' + encodeURIComponent(sid) +
        '&ids=' + encodeURIComponent(ids.join(',')), { method:'POST' });
      toast(r.message || ('已处理 ' + ids.length + ' 条'));
    }
  } catch (e) { toast(e.message, 'err'); }
  await loadStaged();
  await refreshCounts();
}

/* ---------------- 回收站 ---------------- */
async function loadTrash() {
  const box = $('#trash-list');
  try {
    const d = await api('/api/trash?limit=200');
    const items = d.items || [];
    state.trash = items;
    if (!items.length) { box.innerHTML = empty('回收站是空的', '删掉的记忆会先落在这里'); return; }
    box.innerHTML = '<table><thead><tr><th style="width:46%">内容</th><th>重要度</th><th>来源</th>' +
      '<th style="width:130px">删于</th><th style="width:150px"></th></tr></thead><tbody>' +
      items.map(x =>
        '<tr><td><div class="cell2">' + esc(x.content) + '</div>' +
          '<div class="mono" style="margin-top:5px;font-size:10px;color:var(--ink-3)">' + esc(x.id) + '</div></td>' +
        '<td><span class="imp ' + esc(x.importance) + '">' + (IMP[x.importance] || x.importance) + '</span></td>' +
        '<td class="mono" style="font-size:11px">' + esc(x.agent || '-') + '</td>' +
        '<td class="mono" style="font-size:10.5px;color:var(--ink-3);white-space:nowrap">' +
          esc((x.updated_at || '').slice(0, 16).replace('T', ' ')) + '</td>' +
        '<td><div class="acts">' +
          '<button class="btn btn-sm btn-quiet" data-action="restore" data-id="' + esc(x.id) + '">恢复</button>' +
          '<button class="btn btn-sm btn-danger" data-action="purge" data-id="' + esc(x.id) + '">抹除</button>' +
        '</div></td></tr>').join('') + '</tbody></table>';
  } catch (e) { box.innerHTML = empty('加载失败', e.message); }
}

/* ---------------- 会话 ---------------- */
async function loadSessions() {
  const box = $('#sess-wrap');
  try {
    const [d, m] = await Promise.all([api('/api/sessions'), api('/api/modes').catch(() => ({ modes: MODES }))]);
    MODES = m.modes || MODES;
    state.sessions = d.items || [];
    renderSessions();
  } catch (e) { box.innerHTML = empty('加载失败', e.message); }
}
function renderSessions() {
  const box = $('#sess-wrap');
  const q = ($('#sess-q').value || '').trim().toLowerCase();
  const items = state.sessions.filter(s => !q ||
    (s.id + ' ' + (s.agent || '') + ' ' + (s.project || '') + ' ' + (s.mode || '') + ' ' + (s.view || ''))
      .toLowerCase().indexOf(q) >= 0);
  $('#sess-count').textContent = items.length + ' / ' + state.sessions.length + ' 个会话';
  if (!items.length) { box.innerHTML = empty('没有会话', 'agent 开始读写记忆后就会出现'); return; }
  const modeOpts = k => Object.entries(MODES).map(([id, mm]) =>
    '<option value="' + id + '"' + (id === k ? ' selected' : '') + '>' + esc(mm.label) + '</option>').join('');
  const viewOpts = k => {
    const known = Object.entries(VIEWS_CN).map(([id, l]) =>
      '<option value="' + id + '"' + (id === k ? ' selected' : '') + '>' + l + '</option>').join('');
    return known + ((k && !(k in VIEWS_CN)) ? '<option value="' + esc(k) + '" selected>' + esc(k) + '</option>' : '');
  };
  box.innerHTML = '<table><thead><tr><th>会话</th><th>Agent</th><th>模式</th><th>视角</th>' +
    '<th class="num">召回</th><th class="num">写入</th><th class="num">拦截</th><th>最近</th></tr></thead><tbody>' +
    items.map(s =>
      '<tr><td><span class="sid" data-action="copy" data-copy="' + esc(s.id) + '" title="复制会话 ID">' +
        esc(shortSid(s.id)) + '</span></td>' +
      '<td style="font-size:12px">' + esc(s.agent || '-') + '</td>' +
      '<td><div class="sel-w"><select class="input sel" style="height:25px;font-size:11.5px" data-role="mode" data-sid="' +
        esc(s.id) + '" aria-label="模式">' + modeOpts(s.mode) + '</select></div></td>' +
      '<td><div class="sel-w"><select class="input sel" style="height:25px;font-size:11.5px" data-role="view" data-sid="' +
        esc(s.id) + '" aria-label="视角">' + viewOpts(s.view) + '</select></div></td>' +
      '<td class="num">' + (s.recalls || 0) + '</td>' +
      '<td class="num">' + (s.writes || 0) + '</td>' +
      '<td class="num" style="color:' + (s.blocked ? 'var(--seal)' : 'inherit') + '">' + (s.blocked || 0) + '</td>' +
      '<td class="mono" style="font-size:10.5px;color:var(--ink-3);white-space:nowrap">' + esc(relTime(s.updated_at)) + '</td>' +
      '</tr>').join('') + '</tbody></table>';
}

/* ---------------- 抽屉 ---------------- */
function openDrawer(title, bodyHtml, footHtml) {
  $('#drawer-title').innerHTML = title;
  $('#drawer-body').innerHTML = bodyHtml;
  $('#drawer-foot').innerHTML = footHtml || '<span class="sp"></span><button class="btn btn-sm" data-action="close">关闭</button>';
  $('#drawer').style.display = 'flex';
  $('#scrim').classList.add('on');
  $('#scrim').style.pointerEvents = 'auto';
  $('#drawer-body').scrollTop = 0;
}
function closeDrawer(silent) {
  const d = $('#drawer');
  if (!d || d.style.display === 'none') return;
  d.style.display = 'none';
  $('#scrim').classList.remove('on');
  if (!silent && /^#\/memory\//.test(location.hash)) {
    history.replaceState(null, '', '#/' + state.view);
  }
}
async function openMemory(id, fromRoute) {
  let m = state.cache[id];
  if (!m) {
    try {
      const d = await api('/api/memory?id=' + encodeURIComponent(id));
      m = d.item;
      if (m) state.cache[id] = m;
    } catch (e) { toast(e.message, 'err'); return; }
  }
  if (!m) { toast('找不到这条记忆', 'err'); return; }
  state.cache[id] = m;
  const k = KIND[m.kind] || KIND.conclusion;
  const rows = [
    ['账本', k.label],
    ['来源', (m.agent || '-') + ' · ' + (m.source || '-')],
    ['项目', m.project || '全局'],
    ['标签', (m.tags || []).join('、') || '—'],
    ['命中', String(m.hits || 0)],
    ['创建', m.created_at || ''],
    ['更新', m.updated_at || ''],
    ['会话', m.session_id || '-'],
    ['ID', m.id]
  ];
  let chain = '';
  if (m.supersedes || m.superseded_by) {
    chain = '<div style="margin-top:20px"><div class="col-cap">取代链</div><div class="chain">' +
      (m.supersedes ? '<button class="chain-l" data-action="jump" data-id="' + esc(m.supersedes) +
        '"><b>推翻了</b>' + esc(((state.cache[m.supersedes] || {}).content || m.supersedes)) + '</button>' : '') +
      (m.superseded_by ? '<button class="chain-l" data-action="jump" data-id="' + esc(m.superseded_by) +
        '"><b>被这条取代</b>' + esc(((state.cache[m.superseded_by] || {}).content || m.superseded_by)) + '</button>' : '') +
      '</div></div>';
  }
  openDrawer(
    '<span class="mark k-' + esc(m.kind || 'conclusion') + '">' + k.ch + '</span>' +
    '<span class="imp ' + esc(m.importance) + '">' + (IMP[m.importance] || m.importance) + '</span>' +
    '<span class="mono muted" style="font-size:10px;letter-spacing:.08em">' + esc(relTime(m.created_at)) + '</span>',
    '<div class="dr-text">' + esc(m.content) + '</div>' +
    (m.recurred ? '<div style="margin-top:14px;font:11px/1.6 var(--mono);color:var(--seal)">踩过 ' + m.recurred +
      ' 次' + ((m.recurred_agents || []).length ? ' · ' + esc(m.recurred_agents.join('、')) : '') + '</div>' : '') +
    chain +
    '<dl class="kv">' + rows.map(([a, b]) =>
      '<dt>' + a + '</dt><dd>' + (a === 'ID' ? '<span data-action="copy" data-copy="' + esc(b) +
        '" style="cursor:pointer">' + esc(b) + '</span>' : esc(b)) + '</dd>').join('') + '</dl>',
    '<button class="btn btn-sm btn-danger" data-action="del-mem" data-id="' + esc(m.id) + '">删除</button>' +
    '<span class="sp"></span><button class="btn btn-sm" data-action="close">关闭</button>'
  );
  if (!fromRoute && !/^#\/memory\//.test(location.hash)) {
    history.replaceState(null, '', '#/memory/' + m.id);
  }
}
async function openEvents(gi) {
  const groups = $('#audit-list')._groups || [];
  const g = groups[gi];
  if (!g) return;
  const label = (EV[g.event] || [g.event])[0];
  openDrawer('<span class="mono" style="font-size:11px">' + esc(label) + '</span>' +
    '<span class="mono muted" style="font-size:10px">' + esc(g.agent || '-') + ' · ' + g.items.length + ' 条</span>',
    (g.items.length > 60 ? '<div class="e-s" style="margin-bottom:12px">只显示最近 60 条</div>' : '') +
    g.items.slice(0, 60).map(e =>
      '<div style="padding:11px 0;border-bottom:1px solid var(--rule)">' +
      '<div class="mono" style="font-size:10px;color:var(--ink-3);letter-spacing:.04em">' +
        esc((e.ts || '').slice(0, 19).replace('T', ' ')) +
        (e.session_id ? ' · <span data-action="copy" data-copy="' + esc(e.session_id) + '" style="cursor:pointer">' +
          esc(shortSid(e.session_id)) + '</span>' : '') +
        (e.memory_id ? ' · <span data-action="jump" data-id="' + esc(e.memory_id) + '" style="cursor:pointer;color:var(--seal)">' +
          esc(e.memory_id) + '</span>' : '') +
      '</div>' +
      '<div style="margin-top:5px;font-size:12.5px;line-height:1.7;color:var(--ink-2);white-space:pre-wrap">' +
        esc(e.detail || '（无详情）') + '</div></div>').join(''),
    '<span class="sp"></span><button class="btn btn-sm" data-action="close">关闭</button>');
}

/* ---------------- 询问框 ---------------- */
let askResolve = null;
function ask(title, body, okLabel, danger) {
  return new Promise(resolve => {
    askResolve = resolve;
    const wrap = document.createElement('div');
    wrap.className = 'ask-wrap';
    wrap.id = 'ask';
    wrap.innerHTML = '<div class="ask" role="dialog" aria-modal="true"><h3>' + esc(title) + '</h3>' +
      (body ? '<p>' + esc(body) + '</p>' : '') +
      '<div class="ask-f"><button class="btn btn-sm" data-action="ask-no">取消</button>' +
      '<button class="btn btn-sm ' + (danger ? 'ok-danger' : 'btn-primary') + '" data-action="ask-yes">' +
      esc(okLabel || '确定') + '</button></div></div>';
    document.body.appendChild(wrap);
    $('[data-action="ask-yes"]', wrap).focus();
  });
}
function closeAsk(v) {
  const w = $('#ask');
  if (w) w.remove();
  if (askResolve) { const r = askResolve; askResolve = null; r(v); }
}

/* ---------------- 动作 ---------------- */
async function refreshCounts() {
  try {
    const o = await api('/api/overview');
    state.overview = o;
    const st = await api('/api/staged').catch(() => ({ items: [] }));
    o.staged_n = (st.items || []).length;
    renderNav();
  } catch (e) { /* 静默 */ }
}
async function setMode(mode) {
  try {
    const r = await api('/api/mode?mode=' + encodeURIComponent(mode) + '&scope=default', { method:'POST' });
    toast(r.message || '模式已切换');
    await loadModes();
  } catch (e) { toast(e.message, 'err'); }
}
async function setSessionMode(mode, sid) {
  try {
    const r = await api('/api/mode?scope=session&mode=' + encodeURIComponent(mode) +
      '&session=' + encodeURIComponent(sid), { method:'POST' });
    toast(r.message || '已切换');
  } catch (e) { toast(e.message, 'err'); }
}
async function setSessionView(view, sid) {
  try {
    const r = await api('/api/view?view=' + encodeURIComponent(view) + '&session=' + encodeURIComponent(sid),
      { method:'POST' });
    toast(r.message || '已切换');
    await loadSessions();
  } catch (e) { toast(e.message, 'err'); }
}
async function delMem(id) {
  const ok = await ask('删除这条记忆？', '先落进回收站，随时可以捞回来；审计流水会记下这次删除。', '删除', true);
  if (!ok) return;
  try {
    const r = await api('/api/memory/delete?id=' + encodeURIComponent(id), { method:'POST' });
    toast(r.message || '已删除');
    delete state.cache[id];
    closeDrawer(true);
    if (state.view === 'memories') await loadMemories();
    await refreshCounts();
  } catch (e) { toast(e.message, 'err'); }
}
async function restoreMem(id) {
  try {
    const r = await api('/api/memory/restore?id=' + encodeURIComponent(id), { method:'POST' });
    toast(r.message || '已恢复');
    await loadTrash(); await refreshCounts();
  } catch (e) { toast(e.message, 'err'); }
}
async function purgeMem(id) {
  const ok = await ask('彻底抹除？', '物理删除这条记忆，并清掉审计里对它的内容摘录。这一步之后没有撤销。', '抹除', true);
  if (!ok) return;
  try {
    const r = await api('/api/memory/purge?id=' + encodeURIComponent(id), { method:'POST' });
    toast(r.message || '已抹除');
    await loadTrash(); await refreshCounts();
  } catch (e) { toast(e.message, 'err'); }
}
async function stopServer() {
  const ok = await ask('停止审计台服务？', '页面会断开。双击桌面上的「OwnMem 审计台」即可重新启动。', '停止服务', true);
  if (!ok) return;
  try { await api('/api/shutdown', { method:'POST' }); } catch (e) { /* 正在退出 */ }
  document.body.innerHTML = '<div class="stopped"><div><h2>服务已停止</h2>' +
    '<p>DOUBLE-CLICK THE DESKTOP LAUNCHER TO RESTART</p></div></div>';
}

document.addEventListener('click', e => {
  const el = e.target.closest('[data-action]');
  if (!el) return;
  const act = el.dataset.action;
  if (act === 'nav') { go('/' + el.dataset.view); return; }
  if (act === 'close') { closeDrawer(); closeAsk(false); return; }
  if (act === 'copy') { copyText(el.dataset.copy); return; }
  if (act === 'jump') { history.replaceState(null, '', '#/memory/' + el.dataset.id); openMemory(el.dataset.id, true); return; }
  if (act === 'refresh') { showView(state.view); return; }
  if (act === 'stop') { stopServer(); return; }
  if (act === 'set-mode') { setMode(el.dataset.mode); return; }
  if (act === 'open-mem') { openMemory(el.dataset.id); return; }
  if (act === 'del-mem') { delMem(el.dataset.id); return; }
  if (act === 'restore') { restoreMem(el.dataset.id); return; }
  if (act === 'purge') { purgeMem(el.dataset.id); return; }
  if (act === 'mem-kind') { state.mem.kind = el.dataset.kind; renderKindSeg(); loadMemories(); return; }
  if (act === 'mem-more') { loadMemories(true); return; }
  if (act === 'mem-reset') {
    state.mem = { q:'', kind:'', imp:'', agent:'', offset:0, items:[], total:0, ranked:false };
    $('#mem-q').value = ''; $('#mem-imp').value = ''; $('#mem-agent').value = '';
    renderKindSeg(); loadMemories(); return;
  }
  if (act === 'audit-refresh') { loadAudit(); return; }
  if (act === 'audit-more') { loadAudit(true); return; }
  if (act === 'audit-key') {
    state.audit.key = !state.audit.key;
    el.setAttribute('aria-pressed', String(state.audit.key));
    loadAudit(); return;
  }
  if (act === 'audit-live') {
    state.audit.live = !state.audit.live;
    el.setAttribute('aria-pressed', String(state.audit.live));
    if (state.audit.live) { startLive(); loadAudit(); toast('每 5 秒自动追一次'); }
    else stopLive();
    return;
  }
  if (act === 'open-ev') { openEvents(Number(el.dataset.gi)); return; }
  if (act === 'staged-all') {
    const boxes = $$('.js-st');
    const all = boxes.every(b => b.checked);
    boxes.forEach(b => { b.checked = !all; });
    syncBulk(); return;
  }
  if (act === 'staged') { stagedAction(el.dataset.kind); return; }
  if (act === 'trash-refresh') { loadTrash(); return; }
  if (act === 'sess-refresh') { loadSessions(); return; }
  if (act === 'ask-yes') { closeAsk(true); return; }
  if (act === 'ask-no') { closeAsk(false); return; }
});
document.addEventListener('change', e => {
  const el = e.target;
  if (el.id === 'mem-imp') { state.mem.imp = el.value; loadMemories(); }
  else if (el.id === 'mem-agent') { state.mem.agent = el.value; loadMemories(); }
  else if (el.id === 'audit-event') { state.audit.event = el.value; loadAudit(); }
  else if (el.classList && el.classList.contains('js-st')) { syncBulk(); }
  else if (el.dataset && el.dataset.role === 'mode') { setSessionMode(el.value, el.dataset.sid); }
  else if (el.dataset && el.dataset.role === 'view') { setSessionView(el.value, el.dataset.sid); }
});
$('#scrim').addEventListener('click', () => closeDrawer());

/* 搜索防抖 */
function debounce(fn, ms) {
  let t = null;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
const memSearch = debounce(() => { state.mem.q = $('#mem-q').value.trim(); loadMemories(); }, 240);
$('#mem-q').addEventListener('input', memSearch);
$('#sess-q').addEventListener('input', debounce(renderSessions, 160));
$('#audit-session').addEventListener('keydown', e => {
  if (e.key === 'Enter') { state.audit.session = e.target.value.trim(); loadAudit(); }
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if ($('#ask')) { closeAsk(false); return; }
    if ($('#drawer').style.display !== 'none') { closeDrawer(); return; }
    return;
  }
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'select' || tag === 'textarea') return;
  if (e.key === '/') {
    e.preventDefault();
    if (state.view !== 'memories' && state.view !== 'sessions') {
      go('/memories');
      setTimeout(() => { const b = $('#mem-q'); if (b) b.focus(); }, 80);
    } else {
      const b = state.view === 'sessions' ? $('#sess-q') : $('#mem-q');
      if (b) b.focus();
    }
    return;
  }
  if (/^[1-6]$/.test(e.key)) { go('/' + VIEWS[Number(e.key) - 1].id); return; }
  if (e.key === 'r' && !e.metaKey && !e.ctrlKey) { showView(state.view); }
});

/* 主题 */
const THEME_KEY = 'ownmem-theme';
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  const btn = $('#theme-btn');
  if (!btn) return;
  btn.innerHTML = icon(t === 'dark' ? 'sun' : 'moon', 14);
  btn.title = t === 'dark' ? '换成纸白' : '换成墨黑';
}
function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
}
$('#theme-btn').addEventListener('click', toggleTheme);

/* 启动 */
window.addEventListener('hashchange', route);
(async function boot() {
  applyTheme(document.documentElement.dataset.theme || 'dark');
  hydrate();
  $('#refresh-btn').innerHTML = icon('refresh', 14);
  renderNav();
  renderKindSeg();
  renderEventOptions();
  try {
    const meta = await api('/api/meta');
    if (meta.events) Object.keys(meta.events).forEach(k => {
      if (EV[k]) EV[k][0] = meta.events[k]; else EV[k] = [meta.events[k], ''];
    });
    if (meta.kinds) Object.keys(meta.kinds).forEach(k => { if (KIND[k]) KIND[k].label = meta.kinds[k]; });
    if (meta.views) Object.keys(meta.views).forEach(k => { VIEWS_CN[k] = meta.views[k]; });
  } catch (e) { /* 内置兜底文案够用 */ }
  renderEventOptions();
  await loadOverview().catch(e => toast(e.message, 'err'));
  route();
})();
</script>
</body>
</html>
"""
