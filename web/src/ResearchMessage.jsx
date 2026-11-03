import React from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

export function ResearchMessage({message, t, onSource}) {
  const citations=message.metadata?.citations||[];
  const content=typeof message.content==='string'?message.content:message.content?.text||'';
  const marked=content.replace(/\[(\d+)\](?!\()/g,(token,n)=>citations[Number(n)-1]?`[${n}](#citation-${n})`:token);
  return <article className={'message '+message.role}>
    <strong>{message.role==='user'?t('你','You'):'VoxQuarry'}</strong>
    <div className="markdown"><Markdown remarkPlugins={[remarkGfm]} skipHtml components={{
      img:({alt})=><span>{alt}</span>,
      a:({href,children})=>href?.startsWith('#citation-')?<button className="citation" onClick={()=>onSource(citations[Number(href.slice(10))-1])}>{children}</button>:<a href={href} target={href?.startsWith('http')?'_blank':undefined} rel="noopener noreferrer">{children}</a>
    }}>{marked}</Markdown></div>
    {citations.length>0&&<div className="citation-list">{citations.map((c,i)=><button key={c.citation_id||c.source_id||i} onClick={()=>onSource(c)}>[{i+1}] {t('查看来源','View source')}</button>)}</div>}
  </article>;
}

export function ResearchRuns({runs,t,onCancel,onResume}) {
  return <div className="research-runs">{(runs||[]).map(run=><details key={run.run_id}><summary>{t('研究记录','Research run')} · {t(({completed:'已完成',running:'进行中',queued:'排队中',blocked:'需要处理',failed:'失败',cancelled:'已停止'})[run.status]||run.status,run.status)}</summary>
    <ol>{(run.steps||[]).map((step,i)=><li key={step.step_id||i}>{t(step.title||'研究步骤',step.tool_name||`Step ${i+1}`)} · {step.status}</li>)}</ol>
    {['queued','running'].includes(run.status)&&<button onClick={()=>onCancel(run.run_id)}>{t('停止研究','Stop research')}</button>}
    {['blocked','failed'].includes(run.status)&&<button onClick={()=>onResume(run.run_id)}>{t('继续研究','Resume research')}</button>}
  </details>)}</div>;
}
