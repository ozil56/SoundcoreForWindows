"""User-facing instructions for preparing traceable project vocabulary with an agent."""
from pathlib import Path
import tkinter as tk
from tkinter import ttk,filedialog

BEST_PRACTICES='''专用词库是识别提示，不是原文替换表。

1. 先确定资料范围
选择这次会议所在项目的资料目录，并补充会议主题。优先读取最新定稿、会议议程、审查意见、设计说明、人员名单和术语表；旧版本用于核对，不凭文件名猜词。

2. 只收集值得提示的词
优先项目名、地名、桥梁/互通等工程实体、人名、机构名、软件名、产品型号和专业术语。保留完整专名，筛掉“项目、会议、问题、设计”等泛词、整句话、随机编号和无关数字。常用跨项目术语可另行审核后归入通用词库。

3. 每个词都要有依据
记录相对文件路径、页码/段落/表格位置以及简短原文。不能从文件名推断人名，不能自动将拼音或同音字当作正式名称。扫描件 OCR、冲突写法和低置信词单独进入待审核清单。

4. 保留规范写法，合理排序
同一实体优先采用最新正式资料的写法。原文中确实使用的全称和简称各占一行；不要在一行写“全称=简称”或“正确词|错别字”。去重但不误合并不同实体，保留词内空格、英文大小写和型号。最重要、最可能在会议里出现的专名放前面。

5. 少而准，允许逐步增加
不追求凑满固定数量。可先整理约 50–300 个高相关词；资料较多时保留更多有证据的术语，分项目维护。程序会检索整份词库，再按音频段选取有限提示；词库越长不等于全部塞进每段 Prompt。专用词库靠前的少量词会作为初始提示，因此排序有用。

6. 文件格式与交付物
可导入文件：UTF-8 TXT，每行一个词或完整短语。不要编号、标题、解释、逗号列表或 Markdown 代码围栏。词内空格保留，不以空格分词。
例如：
盐靖高速
施工图审查
Qwen3-ASR

另存“来源核对.tsv”和“待审核.txt”。来源、说明和疑问不要混进正式词库。待审核文件也保持每行一个候选词，疑问写在来源表。

7. 在本工具中使用
专用词库 → 添加或打开项目 → 导入 TXT → 预览并保存。候选词导入时勾选“先存入待审核”，核对后筛选“待审核”，点击“恢复 / 审核通过”。
自助转写 → 选择录音 → 选择项目专用词库 → 开始转写。通用词库可同时启用；旧转写始终保留。日常自动转写使用通用词库，不会猜测项目归属。

8. 用真实片段验证
先用 5–10 分钟有专名的音频比较效果，检查是否把普通词错误引导成专名。只把确认无误、以后仍有用的词加入长期词库。词库不能代替对姓名、数字、责任和期限的人工核对。
'''


def extraction_prompt(project='',source='',destination='',focus=''):
    project=project.strip() or '【填写项目名】'
    source=source.strip() or '【填写项目资料目录】'
    destination=destination.strip() or '【填写输出目录】'
    focus=focus.strip() or '未限定，请依据项目资料筛选会议中可能出现的专名和术语'
    return f'''请为本地语音识别工具 SoundcoreForWindows 提取一份项目专用词库。

项目：{project}
资料目录：{source}
输出目录：{destination}
本次会议主题/关注范围：{focus}

用途：为 Qwen3-ASR 提供领域专名提示，不做全文替换，不把识别错误直接固化为术语。

请执行以下工作：
1. 先列出目录内可读的文档类资料及其版本/日期。优先最新正式 DOCX、DOC、PDF、PPTX、XLSX、TXT 等，跳过音频、视频、安装包、缓存、重复副本和明显无关内容。只读原始资料，不修改、移动或删除原文件；不自动上传资料到外部服务。遵守本机 AGENTS.md 和文档读取规范；若需将 Word 转成 PDF，使用 Microsoft Word COM ExportAsFixedFormat。
2. 读取正文、表格、图注和必要的附录，按需 OCR 扫描件。不要仅凭文件名或目录名生成词条。说明实际读取了哪些文件、哪些文件无法读取；不要把未读取的内容写成已核对。
3. 提取确实出现在资料中的项目名、地名、工程实体、人员/机构名、软件/产品型号、专业术语。保留完整专名。排除泛词、整句话、无关数字、流水号、页眉页脚噪声。会议主题相关性优先于词频，不要求凑满数量。
4. 每个入选词附可追溯证据：相对路径、页码或段落/表格/工作表定位、简短原文摘录。采用最新正式资料中的规范写法；全称和确实出现的简称可以分别保留。保留词内空格、大小写和型号，不臆造同音字、译名或拼音名称。冲突写法、OCR 不确定内容和证据不足的词放入待审核清单，不进入正式词库。
5. 精确去重，保留不同实体。按“会议关键专名 → 高相关专业术语 → 其他相关实体”排序，最关键的少量词放前面。若提供已有通用词库/项目词库，先比对并说明新增、重复和冲突；不要覆盖旧词库。

交付以下三个文件（同名文件存在时使用新版本文件名，不覆盖）：
A. “{project}-专用词库.txt”：UTF-8，每行一个词或完整短语。只能有词条，不含标题、编号、解释、出处、分隔符列表或 Markdown 围栏；不按空格拆词；不要使用“全称=简称”“正确词|错别字”的格式。
B. “{project}-来源核对.tsv”：词条、分类、状态（可用/待审核）、来源相对路径、定位、原文摘录、备注。表格中的换行和制表符做必要清理。
C. “{project}-待审核.txt”：仅候选词，每行一个；疑问和冲突理由写在来源核对表中。无候选时生成空文件并说明。

完成后报告正式词条数、待审核数、资料覆盖范围及未解决问题，并给出三个文件路径。不要自动改动软件的词库数据库，也不要自动重转写录音。
导入时：正式词库导入对应项目；候选清单勾选“先存入待审核”。使用时：自助转写中选择录音、通用词库（可选）和本项目专用词库，重新转写并保留旧结果。
'''


def show_guide(parent,project=''):
    window=tk.Toplevel(parent);window.title('AI 提取专用词库指南');window.geometry('900x720');window.minsize(780,650)
    body=ttk.Frame(window,padding=16);body.pack(fill='both',expand=True)
    ttk.Label(body,text='AI 提取词库 · 最佳实践与 Prompt',font=('Microsoft YaHei UI',15,'bold')).pack(anchor='w',pady=(0,12))
    tabs=ttk.Notebook(body);tabs.pack(fill='both',expand=True)
    practice=ttk.Frame(tabs,padding=12);tabs.add(practice,text='最佳实践与格式')
    prompt_tab=ttk.Frame(tabs,padding=12);tabs.add(prompt_tab,text='生成与复制 Prompt')
    def text_area(parent,content=''):
        frame=ttk.Frame(parent);frame.pack(fill='both',expand=True)
        text=tk.Text(frame,wrap='word',font=('Microsoft YaHei UI',10),height=10,padx=8,pady=8)
        scroll=ttk.Scrollbar(frame,orient='vertical',command=text.yview)
        text.configure(yscrollcommand=scroll.set);scroll.pack(side='right',fill='y');text.pack(fill='both',expand=True)
        text.insert('1.0',content);text.configure(state='disabled');return text
    text_area(practice,BEST_PRACTICES)
    values={}
    for key,label,default in [('project','项目名称',project),('source','项目资料目录',''),
            ('destination','词库输出目录',''),('focus','会议主题（可选）','')]:
        row=ttk.Frame(prompt_tab);row.pack(fill='x',pady=(0,6))
        ttk.Label(row,text=label,width=18).pack(side='left')
        value=tk.StringVar(value=default);values[key]=value
        ttk.Entry(row,textvariable=value).pack(side='left',fill='x',expand=True)
        if key in ('source','destination'):
            def choose(var=value):
                path=filedialog.askdirectory(parent=window)
                if path:var.set(path)
            ttk.Button(row,text='选择…',command=choose).pack(side='left',padx=(8,0))
    ttk.Label(prompt_tab,text='可先复制模板，再让 Agent 补全占位内容；本页不会读取或上传项目文件。').pack(anchor='w',pady=(0,8))
    preview=text_area(prompt_tab)
    def render(*_):
        value=extraction_prompt(**{key:var.get() for key,var in values.items()})
        preview.configure(state='normal');preview.delete('1.0','end');preview.insert('1.0',value);preview.configure(state='disabled')
        return value
    for var in values.values():var.trace_add('write',render)
    render();status=tk.StringVar()
    def copy():
        window.clipboard_clear();window.clipboard_append(render());status.set('Prompt 已复制，可粘贴到 AI / Codex。')
    def save():
        target=filedialog.asksaveasfilename(parent=window,initialfile='专用词库提取-Prompt.txt',defaultextension='.txt',filetypes=[('文本','*.txt')])
        if target:Path(target).write_text(render(),encoding='utf-8-sig');status.set('Prompt 已保存。')
    bar=ttk.Frame(body);bar.pack(fill='x',pady=(12,0))
    ttk.Button(bar,text='复制 Prompt',command=copy).pack(side='left')
    ttk.Button(bar,text='保存 Prompt…',command=save).pack(side='left',padx=8)
    ttk.Label(bar,textvariable=status).pack(side='left',padx=6)
    ttk.Button(bar,text='关闭',command=window.destroy).pack(side='right')
    return window
