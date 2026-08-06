// 中文加粗门。`**术语（英文）**中文` 这种写法在 CommonMark 里会把 ** 原样吐出来，
// remark-cjk-friendly 治的就是它。用例取自真实回答里出现过的形态。
//
// 走 App.tsx 用的那条渲染路径（ReactMarkdown），而不是自己拼 unified 管线——否则插件
// 装了但没接进组件也测不出来。插件表在这里重写了一遍，一致性靠文件末尾的文本对账保证。
import { readFile } from 'node:fs/promises'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import remarkCjkFriendly from 'remark-cjk-friendly'
import rehypeKatex from 'rehype-katex'

// 已知不覆盖：GFM 删除线（`~~删除线（英文）~~中文`）。remark-cjk-friendly 明确不管它，
// 要另装 remark-cjk-friendly-gfm-strikethrough。所以「本门通过」不等于这类问题全清了。
const CASES = [
  '用基于虚拟运行时间的**衰减机制（decaying）**处理睡眠任务的 lag。',
  '第**6.6版本（2024年）**开始用 EEVDF。',
  '第**6.6版本(2024年)**开始用 EEVDF。',
  '算出**虚拟截止期限（virtual deadline, VD）**，然后执行。',
  '让**同优先级**的任务平分 CPU。',
  '**根本原因**：平均周转时间反映的是每个作业的时长。',
  '把**「护航效应」**讲清楚。',
]

const render = source => renderToStaticMarkup(
  React.createElement(ReactMarkdown, {
    remarkPlugins: [remarkGfm, remarkMath, remarkCjkFriendly],
    rehypePlugins: [rehypeKatex],
  }, source),
)

const failed = CASES.map(source => ({ source, html: render(source) }))
  .filter(({ html }) => !html.includes('<strong>'))

if (failed.length) {
  console.error(`中文加粗门失败 ${failed.length}/${CASES.length} 例：`)
  for (const { source, html } of failed) console.error(`  源: ${source}\n  出: ${html}`)
  process.exit(1)
}

// 公式用例守 REHYPE_PLUGINS 的内容。只对账数量的话，把它清成 [] 门也是绿的，
// 而那时 KaTeX 静默失效、公式以 $...$ 原样上屏。
const formula = render('平均周转 $T = \\frac{110}{3}$ 秒。')
if (!formula.includes('katex')) {
  console.error(`公式渲染失败：rehype 插件没生效，KaTeX 不会上屏\n  出: ${formula.trim()}`)
  process.exit(1)
}

// 上面证明插件有效，下面证明它真的接进了组件——只测前者的话，
// 插件从 App.tsx 的插件表里被删掉这道门也不会红。
//
// 剥掉注释再扫，否则「新增 <ReactMarkdown> 渲染点时记得…」这种说明性注释会把门弄红。
// 顺序很重要：先抹字符串，再无条件剥 //。反过来的话，含 '//' 的字符串会让整行剩余部分
// 被当成注释抹掉——那不只是误红，渲染点整行被抹时 points 和 wired 一起少 1，
// 一个漏接插件的渲染点就安然过门了。抹成等长空格，行号不受影响。
const blank = hit => hit.replace(/[^\n]/g, ' ')
const app = (await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8'))
  .replace(/\/\*[\s\S]*?\*\//g, blank)
  .replace(/'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*"|`(?:[^`\\]|\\.)*`/g, blank)
  .replace(/\/\/.*$/gm, blank)

// 两张表的表体都要查内容。上面那条公式用例只能证明 rehype-katex 这个插件有效——
// 它用的是本文件声明的插件表，把 App.tsx 里的 REHYPE_PLUGINS 清空它照样绿。
// 表体取到语句末尾而不是第一个 ]，否则 [remarkGfm, [plugin, opts]] 这种嵌套会被截断。
for (const [name, must] of [['REMARK_PLUGINS', 'remarkCjkFriendly'], ['REHYPE_PLUGINS', 'rehypeKatex']]) {
  const table = app.match(new RegExp(`const ${name} = (.*)$`, 'm'))
  if (!table) {
    console.error(`接线检查失败：App.tsx 里找不到 ${name}`)
    process.exit(1)
  }
  if (!table[1].includes(must)) {
    console.error(`接线检查失败：${name} 里没有 ${must}（当前是 ${table[1].trim()}）`)
    process.exit(1)
  }
}

// 两张表都要对账：只守 remark 的话，新增渲染点漏写 rehypePlugins 时 KaTeX 会静默失效，
// 公式以 $...$ 原样上屏，而门是绿的。
const points = (app.match(/<ReactMarkdown/g) ?? []).length
for (const [attr, shared] of [['remarkPlugins', 'REMARK_PLUGINS'], ['rehypePlugins', 'REHYPE_PLUGINS']]) {
  const wired = [...app.matchAll(new RegExp(`${attr}=\\{([^}]*)\\}`, 'g'))].map(m => m[1].trim())
  const stray = wired.filter(value => value !== shared)
  if (stray.length) {
    console.error(`接线检查失败：有 ${stray.length} 处 ${attr} 没走 ${shared}：${stray.join(' , ')}`)
    process.exit(1)
  }
  if (points === 0 || points !== wired.length) {
    console.error(`接线检查失败：${points} 处 <ReactMarkdown> 里只有 ${wired.length} 处写了 ${attr}`)
    process.exit(1)
  }
}
console.log(`中文加粗门通过 ${CASES.length}/${CASES.length} 例；${points} 处渲染点的两张插件表都接对了`)
