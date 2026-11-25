import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { listen, type UnlistenFn } from '@tauri-apps/api/event';
import { open } from '@tauri-apps/plugin-dialog';
import { getCurrentWindow } from '@tauri-apps/api/window';
import {
  Activity, ArrowRight, ArrowUp, Bot,
  CheckCircle2, ChevronDown, ChevronRight, ClipboardList, Code2, Cpu,
  FileCode2, FileText, Folder, FolderOpen, Gauge, GitBranch, Globe2, History,
  Info, Layers3, LockKeyhole, Maximize2, MessageSquare, Minus, MoreHorizontal,
  PanelRight, Plus, RefreshCw, Search, Settings2, Shield, Sparkles,
  Terminal, X, LoaderCircle, Wrench, Users, WandSparkles, BrainCircuit,
  BotMessageSquare, SlidersHorizontal, Save, Trash2, Play,
} from 'lucide-react';

type DaemonStatus = { installed: boolean; version: string; detail: string };
type WorkspaceEntry = { name: string; path: string; kind: string; size: number };
type TaskOutput = { run_id: string; stream: string; line: string };
type TaskFinished = { run_id: string; success: boolean; code: number | null };
type ChatMessage = { id: string; role: 'user' | 'daemon'; content: string; mode?: string; status?: 'running' | 'done' | 'error' };
type ActivityItem = { id: string; label: string; detail: string; time: string; tone?: string };
type CustomAgent = { id: string; name: string; specialty: string; model: string; instructions: string; createdAt: string };
const starterAgents: CustomAgent[] = [
  { id: 'architect', name: 'Architect', specialty: 'System design', model: 'Default provider', instructions: 'Act as a principal software architect. Inspect the project before making recommendations. Explain trade-offs, boundaries, data flow, and a practical implementation plan. Do not edit files unless explicitly asked.', createdAt: 'Built in' },
  { id: 'reviewer', name: 'Reviewer', specialty: 'Code review', model: 'Default provider', instructions: 'Review changes for correctness, security, performance, and maintainability. Prioritize actionable findings with file paths and severity. Do not invent issues.', createdAt: 'Built in' },
  { id: 'debugger', name: 'Debugger', specialty: 'Root-cause analysis', model: 'Default provider', instructions: 'Reproduce the issue, inspect relevant code and logs, test hypotheses, identify the root cause, and verify any fix. Never claim a test passed unless it ran.', createdAt: 'Built in' },
  { id: 'researcher', name: 'Researcher', specialty: 'Technical research', model: 'Default provider', instructions: 'Research the question carefully. Prefer primary sources, distinguish evidence from inference, cite sources, and remain read-only unless explicitly asked otherwise.', createdAt: 'Built in' },
];

const formatBytes = (bytes: number) => bytes < 1024 ? `${bytes} B` : bytes < 1024 ** 2 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1024 ** 2).toFixed(1)} MB`;
const timeNow = () => new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

function Mark({ small = false }: { small?: boolean }) {
  return <div className={`daemon-mark ${small ? 'small' : ''}`} aria-hidden="true"><svg viewBox="0 0 40 40"><path d="M20 3 37 34H25l-5-9-5 9H3L20 3Z" fill="currentColor"/><path d="m20 14 5 9H15l5-9Z" fill="#171714"/></svg></div>;
}

function App() {
  const [workspace, setWorkspace] = useState(() => localStorage.getItem('daemon:last-workspace') || '');
  const [entries, setEntries] = useState<WorkspaceEntry[]>([]);
  const [browsePath, setBrowsePath] = useState('');
  const [daemon, setDaemon] = useState<DaemonStatus>({ installed: false, version: 'Checking…', detail: '' });
  const [git, setGit] = useState('');
  const [activeView, setActiveView] = useState<'agent' | 'explorer' | 'activity' | 'settings' | 'agents' | 'tools'>('agent');
  const [customAgents, setCustomAgents] = useState<CustomAgent[]>(() => { try { return JSON.parse(localStorage.getItem('daemon:agents') || '[]'); } catch { return []; } });
  const [selectedAgent, setSelectedAgent] = useState('default');
  const [agentDraft, setAgentDraft] = useState({ name: '', specialty: '', model: 'Default provider', instructions: '' });
  const [agentModal, setAgentModal] = useState(false);
  const [generatingAgent, setGeneratingAgent] = useState(false);
  const [toolOutput, setToolOutput] = useState('');
  const [toolBusy, setToolBusy] = useState(false);
  const [mode, setMode] = useState<'build' | 'plan' | 'research'>('build');
  const [prompt, setPrompt] = useState('');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [running, setRunning] = useState(false);
  const [runId, setRunId] = useState('');
  const [activities, setActivities] = useState<ActivityItem[]>([]);
  const [preview, setPreview] = useState<{ name: string; path: string; content: string } | null>(null);
  const [rightTab, setRightTab] = useState<'context' | 'activity'>('context');
  const [utility, setUtility] = useState<{ title: string; output: string } | null>(null);
  const [utilityBusy, setUtilityBusy] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchTerm, setSearchTerm] = useState('');
  const [notice, setNotice] = useState('');
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const runIdRef = useRef('');
  const workspaceRef = useRef('');
  useEffect(() => { runIdRef.current = runId; workspaceRef.current = workspace; }, [runId, workspace]);

  const allAgents = useMemo(() => [...starterAgents, ...customAgents], [customAgents]);
  useEffect(() => { localStorage.setItem('daemon:agents', JSON.stringify(customAgents)); }, [customAgents]);

  const addActivity = useCallback((label: string, detail: string, tone = 'neutral') => {
    setActivities(prev => [{ id: `${Date.now()}-${Math.random()}`, label, detail, time: timeNow(), tone }, ...prev].slice(0, 18));
  }, []);

  const refreshWorkspace = useCallback(async (path: string) => {
    if (!path) return;
    setWorkspace(path);
    setPreview(null);
    try {
      setBrowsePath(path);
      const [files, gitStatus] = await Promise.all([
        invoke<WorkspaceEntry[]>('list_workspace', { workspace: path, subpath: path }),
        invoke<string>('git_status', { workspace: path }),
      ]);
      setEntries(files);
      setGit(gitStatus || 'Clean working tree');
      addActivity('Workspace opened', path, 'green');
    } catch (error) {
      setEntries([]); setGit('');
      localStorage.removeItem('daemon:last-workspace');
      setNotice(String(error));
    }
  }, [addActivity]);

  useEffect(() => {
    if (workspace) localStorage.setItem('daemon:last-workspace', workspace);
  }, [workspace]);

  useEffect(() => {
    const saved = localStorage.getItem('daemon:last-workspace');
    if (saved) refreshWorkspace(saved);
  }, [refreshWorkspace]);

  useEffect(() => {
    invoke<DaemonStatus>('daemon_status').then(setDaemon).catch(error => setDaemon({ installed: false, version: 'Not available', detail: String(error) }));
    let active = true;
    let unlistenOutput: UnlistenFn | undefined;
    let unlistenFinished: UnlistenFn | undefined;
    listen<TaskOutput>('daemon-output', ({ payload }) => {
      const current = runIdRef.current;
      if (current && payload.run_id !== current) return;
      if (!current) { runIdRef.current = payload.run_id; setRunId(payload.run_id); }
      setMessages(prev => prev.map(message => message.id === payload.run_id || (message.role === 'daemon' && message.status === 'running' && message.id.startsWith('pending-'))
        ? { ...message, id: payload.run_id, content: `${message.content && message.content !== 'Starting Daemon…' ? `${message.content}\n` : ''}${payload.line}` }
        : message));
    }).then(fn => { if (active) unlistenOutput = fn; else fn(); });
    listen<TaskFinished>('daemon-complete', ({ payload }) => {
      const current = runIdRef.current;
      if (current && payload.run_id !== current) return;
      if (!current) { runIdRef.current = payload.run_id; setRunId(payload.run_id); }
      setRunning(false);
      setMessages(prev => prev.map(message => message.id === payload.run_id || (message.role === 'daemon' && message.status === 'running' && message.id.startsWith('pending-')) ? { ...message, id: payload.run_id, content: message.content === 'Starting Daemon…' ? 'Daemon finished without returning text output.' : message.content, status: payload.success ? 'done' : 'error' } : message));
      addActivity(payload.success ? 'Run completed' : 'Run stopped', payload.success ? 'Daemon finished the task' : `Exit code: ${payload.code ?? 'unknown'}`, payload.success ? 'green' : 'orange');
      const currentWorkspace = workspaceRef.current;
      if (currentWorkspace) invoke<string>('git_status', { workspace: currentWorkspace }).then(setGit).catch(() => undefined);
    }).then(fn => { if (active) unlistenFinished = fn; else fn(); });
    return () => { active = false; unlistenOutput?.(); unlistenFinished?.(); };
  }, [addActivity]);

  useEffect(() => { messagesEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }); }, [messages]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key === '1') { event.preventDefault(); setActiveView('agent'); setPreview(null); }
      if ((event.ctrlKey || event.metaKey) && event.key === '2') { event.preventDefault(); setActiveView('explorer'); }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); setSearchOpen(true); }
      if (event.key === 'Escape') { setSearchOpen(false); if (!utilityBusy) setUtility(null); }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [utilityBusy]);

  const chooseWorkspace = async () => {
    try {
      const picked = await open({ directory: true, multiple: false, title: 'Choose a project workspace' });
      if (typeof picked === 'string') await refreshWorkspace(picked);
    } catch (error) { setNotice(String(error)); }
  };

  const openFile = async (entry: WorkspaceEntry) => {
    if (entry.kind === 'directory') {
      try {
        const nested = await invoke<WorkspaceEntry[]>('list_workspace', { workspace, subpath: entry.path });
        setBrowsePath(entry.path); setEntries(nested); setPreview(null); setActiveView('explorer');
      } catch (error) { setNotice(String(error)); }
      return;
    }
    try {
      const content = await invoke<string>('read_workspace_file', { workspace, filePath: entry.path });
      setPreview({ name: entry.name, path: entry.path, content });
      setActiveView('explorer');
    } catch (error) { setNotice(String(error)); }
  };

  const submitTask = async (text?: string, forcedMode?: 'build' | 'plan' | 'research') => {
    const task = (text ?? prompt).trim();
    const taskMode = forcedMode ?? mode;
    const activeAgent = allAgents.find(agent => agent.id === selectedAgent);
    const agentTask = activeAgent ? `[ACTIVE DAEMON AGENT: ${activeAgent.name}\nSpecialty: ${activeAgent.specialty}\nAgent instructions: ${activeAgent.instructions}\nApply these instructions to the task below while preserving Daemon's permission and safety rules.]\n\nUSER TASK:\n${task}` : task;
    if (!task) { inputRef.current?.focus(); return; }
    if (!workspace) { setNotice('Choose a workspace before starting a task.'); return; }
    if (!daemon.installed) { setNotice("Daemon's runtime is unavailable. Install Daemon and configure a provider first."); return; }
    if (running) return;
    const newRunId = `pending-${Date.now()}`;
    runIdRef.current = '';
    setRunId('');
    setMessages(prev => [...prev, { id: `user-${Date.now()}`, role: 'user', content: task, mode: taskMode }, { id: newRunId, role: 'daemon', content: 'Starting Daemon…', mode: taskMode, status: 'running' }]);
    setPrompt('');
    setRunning(true);
    addActivity(taskMode === 'plan' ? 'Plan started' : taskMode === 'research' ? 'Research started' : 'Agent run started', task, 'orange');
    try {
      const started = await invoke<string>('execute_task', { workspace, prompt: agentTask, mode: taskMode, model: activeAgent?.model && activeAgent.model !== 'Default provider' ? activeAgent.model : null });
      runIdRef.current = started;
      setRunId(started);
      setMessages(prev => prev.map(message => message.id === newRunId ? { ...message, id: started, content: message.content === 'Starting Daemon…' ? '' : message.content } : message));
    } catch (error) {
      setRunning(false);
      setMessages(prev => prev.map(message => message.id === newRunId ? { ...message, content: String(error), status: 'error' } : message));
    }
  };

  const generateAgentInstructions = async () => {
    if (!workspace) { setNotice('Choose a workspace before asking Daemon to generate an agent.'); return; }
    if (!daemon.installed) { setNotice('Daemon must be connected and have a provider configured.'); return; }
    if (!agentDraft.name.trim() || !agentDraft.specialty.trim()) { setNotice('Add an agent name and specialty first.'); return; }
    setGeneratingAgent(true);
    try {
      const generated = await invoke<string>('generate_agent_prompt', { workspace, name: agentDraft.name, specialty: agentDraft.specialty, brief: agentDraft.instructions || `Design a ${agentDraft.specialty} specialist for software engineering.` });
      setAgentDraft(draft => ({ ...draft, instructions: generated }));
      addActivity('Agent instructions generated', agentDraft.name, 'green');
      setNotice('Agent instructions generated through your configured Daemon provider. Review them before saving.');
    } catch (error) { setNotice(String(error)); } finally { setGeneratingAgent(false); }
  };

  const loadToolCatalog = async () => {
    if (!workspace) { setToolOutput('Choose a workspace first so Daemon can inspect its project-level tool configuration.'); return; }
    setToolBusy(true);
    try { const [builtins, servers] = await Promise.all([invoke<string>('run_utility', { workspace, action: 'tools' }), invoke<string>('run_utility', { workspace, action: 'mcp' })]); setToolOutput(`BUILT-IN TOOL REGISTRY\n\n${builtins}\n\n\nMCP SERVERS\n\n${servers}`); addActivity('Tool registry refreshed', 'Built-in tools and MCP servers', 'green'); }
    catch (error) { setToolOutput(String(error)); } finally { setToolBusy(false); }
  };

  const runUtility = async (action: string, title: string) => {
    if (!workspace) { setNotice('Choose a workspace first.'); return; }
    setUtilityBusy(true); setUtility({ title, output: 'Running…' });
    try {
      const output = await invoke<string>('run_utility', { workspace, action });
      setUtility({ title, output: output || 'No output returned.' });
      addActivity(`${title} checked`, 'Command finished', 'green');
    } catch (error) { setUtility({ title, output: String(error) }); }
    finally { setUtilityBusy(false); }
  };

  const windowAction = async (action: 'minimize' | 'toggleMaximize' | 'close') => {
    try {
      const win = getCurrentWindow();
      if (action === 'minimize') await win.minimize();
      if (action === 'toggleMaximize') await win.toggleMaximize();
      if (action === 'close') await win.close();
    } catch { /* Browser preview has no native window controls. */ }
  };

  const filteredEntries = useMemo(() => entries.filter(entry => entry.name.toLowerCase().includes(searchTerm.toLowerCase())), [entries, searchTerm]);
  const gitBranch = git.split('\n')[0]?.replace(/^##\s*/, '') || 'No repository';
  const gitChanges = git.split('\n').filter(line => line && !line.startsWith('##')).length;

  return (
    <div className="app-shell">
      <div className="titlebar" data-tauri-drag-region>
        <div className="titlebar-left"><Mark small /><span>DAEMON</span><i>ENGINEERING ENVIRONMENT</i></div>
        <div className="titlebar-center" data-tauri-drag-region>DAEMON DESKTOP <span className="titlebar-divider">/</span> LOCAL SESSION</div>
        <div className="titlebar-actions">
          <button aria-label="Minimize" onClick={() => windowAction('minimize')}><Minus size={14}/></button>
          <button aria-label="Maximize" onClick={() => windowAction('toggleMaximize')}><Maximize2 size={13}/></button>
          <button aria-label="Close" className="close-window" onClick={() => windowAction('close')}><X size={15}/></button>
        </div>
      </div>

      <div className="app-body">
        <aside className={`left-sidebar ${sidebarOpen ? '' : 'collapsed'}`}>
          <div className="brand-block">
            <div className="brand-emblem"><Mark /></div>
            <div className="brand-word">daemon<span>.</span></div>
            <div className="brand-sub">LOCAL-FIRST ENGINEERING</div>
          </div>
          <button className="workspace-switcher" onClick={chooseWorkspace}>
            <div className="workspace-icon"><FolderOpen size={15}/></div>
            <div className="workspace-label"><small>ACTIVE WORKSPACE</small><strong>{workspace ? workspace.split(/[\\/]/).filter(Boolean).pop() : 'Choose a project'}</strong></div>
            <ChevronDown size={14} className="muted-icon"/>
          </button>

          <div className="side-section-label">WORKSPACE</div>
          <nav className="primary-nav">
            <button className={activeView === 'agent' ? 'active' : ''} onClick={() => { setActiveView('agent'); setPreview(null); }}><MessageSquare size={16}/><span>Agent</span><kbd>Ctrl 1</kbd></button>
            <button className={activeView === 'explorer' ? 'active' : ''} onClick={() => setActiveView('explorer')}><Folder size={16}/><span>Explorer</span><kbd>Ctrl 2</kbd></button>
            <button className={activeView === 'agents' ? 'active' : ''} onClick={() => setActiveView('agents')}><Users size={16}/><span>Agents</span><b className="nav-count">{allAgents.length}</b></button>
            <button className={activeView === 'tools' ? 'active' : ''} onClick={() => { setActiveView('tools'); loadToolCatalog(); }}><Wrench size={16}/><span>Tools</span></button>
            <button className={activeView === 'activity' ? 'active' : ''} onClick={() => setActiveView('activity')}><Activity size={16}/><span>Activity</span>{activities.length > 0 && <b className="nav-count">{activities.length}</b>}</button>
          </nav>

          <div className="side-divider"/>
          <div className="side-section-head"><span>PROJECT FILES</span><button onClick={() => workspace && refreshWorkspace(workspace)} title="Refresh files"><RefreshCw size={13}/></button></div>
          <div className="file-list">
            {!workspace && <div className="sidebar-empty">Your workspace files will appear here.</div>}
            {filteredEntries.map(entry => <button key={entry.path} className={`file-row ${preview?.path === entry.path ? 'selected' : ''}`} onClick={() => openFile(entry)}>
              {entry.kind === 'directory' ? <Folder size={14}/> : <FileCode2 size={14}/>}<span>{entry.name}</span>{entry.kind === 'directory' && <ChevronRight size={12} className="file-chevron"/>}
            </button>)}
            {workspace && filteredEntries.length === 0 && <div className="sidebar-empty">No matching files.</div>}
          </div>
          <div className="sidebar-bottom">
            <div className="engine-card">
              <div className="engine-mark"><Cpu size={17}/></div>
              <div><strong>Daemon Core</strong><span>{daemon.installed ? 'Runtime connected' : 'Runtime not detected'}</span></div>
              <span className={`engine-dot ${daemon.installed ? 'online' : ''}`}/>
            </div>
            <button className={`bottom-nav ${activeView === 'settings' ? 'active' : ''}`} onClick={() => setActiveView('settings')}><Settings2 size={16}/> Settings</button>
            <div className="sidebar-foot"><span>v1.0.0</span><span>BUILT FOR FOCUS</span></div>
          </div>
        </aside>

        <main className="main-column">
          <header className="main-topbar">
            <div className="crumbs"><button className="icon-button sidebar-toggle" onClick={() => setSidebarOpen(v => !v)} title="Toggle sidebar"><PanelRight size={16}/></button><span className="crumb-muted">Workspace</span><ChevronRight size={13}/><strong>{activeView === 'agent' ? 'Agent session' : activeView === 'explorer' ? 'File explorer' : activeView === 'agents' ? 'Agent studio' : activeView === 'tools' ? 'Tool registry' : activeView === 'activity' ? 'Activity log' : 'Settings'}</strong></div>
            <div className="topbar-right">
              <div className={`connection-pill ${daemon.installed ? 'connected' : ''}`}><span/>{daemon.installed ? 'DAEMON READY' : 'DAEMON OFFLINE'}</div>
              <button className="icon-button" onClick={() => setSearchOpen(v => !v)} title="Search files (Ctrl+K)"><Search size={16}/></button>
              <button className="icon-button" onClick={() => setInspectorOpen(v => !v)} title="Toggle context panel"><PanelRight size={16}/></button>
            </div>
          </header>

          <div className="content-layout">
            <section className="work-area">
              {activeView === 'agent' && <>
                <div className="session-bar">
                  <div className="session-title"><span className="session-symbol"><Sparkles size={15}/></span><div><strong>{messages.length ? 'Current session' : 'New session'}</strong><small>{workspace ? workspace : 'No workspace selected'}</small></div></div>
                  <div className="session-actions"><span className="branch-chip"><GitBranch size={12}/>{gitBranch}</span><button className="icon-button tiny" disabled={running} onClick={() => { runIdRef.current = ''; setMessages([]); setRunId(''); }} title="New session"><Plus size={15}/></button></div>
                </div>

                <div className={`conversation ${messages.length ? 'has-messages' : ''}`}>
                  {!messages.length ? <div className="welcome-state">
                    <div className="welcome-art" />
                    <div className="welcome-copy">
                      <div className="eyebrow"><span className="eyebrow-line"/>ENGINEERING, WITH INTENT</div>
                      <h1>Make space for<br/><em>good work.</em></h1>
                      <p>A focused workspace for thinking through complex tasks, shaping code, and letting Daemon handle the implementation.</p>
                      <div className="welcome-meta"><span><LockKeyhole size={13}/> Local-first</span><span><Shield size={13}/> Permission-aware</span><span><Layers3 size={13}/> Provider-agnostic</span></div>
                    </div>
                    <div className="starter-grid">
                      <button onClick={() => submitTask('Inspect this codebase and explain its architecture.', 'research')}><span className="starter-icon"><Search size={16}/></span><span><strong>Understand this project</strong><small>Map the structure and key systems</small></span><ArrowUpRightIcon/></button>
                      <button onClick={() => { setMode('plan'); inputRef.current?.focus(); setPrompt('Plan the next improvement for this project.'); }}><span className="starter-icon"><ClipboardList size={16}/></span><span><strong>Plan a change</strong><small>Break a big idea into safe steps</small></span><ArrowUpRightIcon/></button>
                      <button onClick={() => { setMode('build'); inputRef.current?.focus(); setPrompt('Review this project for issues and suggest improvements.'); }}><span className="starter-icon"><Code2 size={16}/></span><span><strong>Work on the code</strong><small>Build, refactor, test, and verify</small></span><ArrowUpRightIcon/></button>
                    </div>
                  </div> : <div className="messages-list">
                    {messages.map(message => <div key={message.id} className={`message ${message.role}`}>
                      <div className="message-avatar">{message.role === 'user' ? 'Y' : <Mark small/>}</div>
                      <div className="message-main">
                        <div className="message-meta"><strong>{message.role === 'user' ? 'You' : 'Daemon'}</strong><span>{message.role === 'user' ? (message.mode || 'build').toUpperCase() : message.status === 'running' ? 'WORKING' : message.status === 'error' ? 'STOPPED' : 'COMPLETED'}</span>{message.role === 'daemon' && message.status === 'running' && <i className="typing-dots"><b/><b/><b/></i>}</div>
                        <pre className="message-content">{message.content || (message.status === 'running' ? 'Waiting for output…' : '')}</pre>
                        {message.role === 'daemon' && message.status === 'done' && <div className="message-foot"><CheckCircle2 size={13}/> Run finished <span>·</span> Review your changes before merging</div>}
                      </div>
                    </div>)}
                    <div ref={messagesEndRef}/>
                  </div>}
                </div>

                <div className="composer-wrap">
                  <div className="composer">
                    <div className="composer-topline"><span className="composer-context"><span className="context-dot"/>{workspace ? workspace.split(/[\\/]/).filter(Boolean).pop() : 'No workspace'}</span><span className="composer-hint">Enter to send <i>·</i> Shift + Enter for a new line</span></div>
                    <div className="agent-select-row"><BrainCircuit size={13}/><span>Agent</span><select value={selectedAgent} onChange={e => setSelectedAgent(e.target.value)}><option value="default">Daemon · Default</option>{allAgents.map(agent => <option key={agent.id} value={agent.id}>{agent.name}</option>)}</select><button onClick={() => setActiveView('agents')}>Manage agents <ArrowRight size={12}/></button></div>
                    <textarea ref={inputRef} value={prompt} onChange={e => setPrompt(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitTask(); } }} placeholder={workspace ? 'Ask Daemon to build, investigate, or explain…' : 'Choose a workspace to begin…'} disabled={running || !workspace} rows={2}/>
                    <div className="composer-bottom"><div className="mode-switcher">
                      <button className={mode === 'build' ? 'selected' : ''} onClick={() => setMode('build')}><Code2 size={13}/> Build</button>
                      <button className={mode === 'plan' ? 'selected' : ''} onClick={() => setMode('plan')}><ClipboardList size={13}/> Plan</button>
                      <button className={mode === 'research' ? 'selected' : ''} onClick={() => setMode('research')}><Globe2 size={13}/> Research</button>
                    </div><div className="composer-controls"><span className="approval-note"><Shield size={12}/> Daemon permission policy</span><button className="send-button" onClick={() => submitTask()} disabled={running || !prompt.trim() || !workspace} title="Send task">{running ? <LoaderCircle size={15} className="spin"/> : <ArrowUp size={17}/>}</button></div></div>
                  </div>
                  <div className="composer-disclaimer">Daemon may change your workspace. Non-interactive runs fail closed when a permission needs your approval.</div>
                </div>
              </>}

              {activeView === 'explorer' && <div className="page-view">
                <div className="page-heading"><div><div className="eyebrow">PROJECT SPACE</div><h2>{preview ? preview.name : 'Explorer'}</h2><p>{preview ? preview.path : browsePath || workspace || 'Select a workspace to browse files.'}</p></div><div className="page-heading-actions">{browsePath && browsePath !== workspace && <button className="soft-button" onClick={async () => { const parent = browsePath.replace(/[\\/][^\\/]+$/, ''); const next = parent.length < workspace.length ? workspace : parent; setBrowsePath(next); setEntries(await invoke<WorkspaceEntry[]>('list_workspace', { workspace, subpath: next })); setPreview(null); }}><ArrowUp size={14}/> Up one level</button>}<button className="soft-button" onClick={chooseWorkspace}><FolderOpen size={14}/> Open workspace</button></div></div>
                {preview ? <div className="code-preview"><div className="preview-toolbar"><span><FileCode2 size={14}/>{preview.name}</span><button onClick={() => setPreview(null)}><X size={14}/> Close preview</button></div><pre>{preview.content}</pre></div> : <div className="explorer-grid">{entries.map(entry => <button key={entry.path} onClick={() => openFile(entry)} className="explorer-card"><span className={`explorer-icon ${entry.kind}`} >{entry.kind === 'directory' ? <Folder size={18}/> : <FileCode2 size={18}/>}</span><strong>{entry.name}</strong><small>{entry.kind === 'directory' ? 'Folder' : formatBytes(entry.size)}</small></button>)}</div>}
              </div>}

              {activeView === 'agents' && <div className="page-view studio-view">
                <div className="page-heading"><div><div className="eyebrow">SPECIALISTS / AGENT STUDIO</div><h2>Build your own agents.</h2><p>Shape focused specialists for your workflow. Runs use the provider configured in Daemon, so credentials stay in its vault.</p></div><button className="primary-button" onClick={() => { setAgentDraft({ name: '', specialty: '', model: 'Default provider', instructions: '' }); setAgentModal(true); }}><Plus size={15}/> Create agent</button></div>
                <div className="agent-studio-banner"><div className="studio-emblem"><WandSparkles size={22}/></div><div><strong>A team, not a pile of prompts.</strong><span>Create a specialist profile, generate its instructions through your configured API provider, then select it in the composer. Profiles guide a task; Daemon retains control of tools and permissions.</span></div><span className="studio-count">{allAgents.length.toString().padStart(2, '0')}<small>AVAILABLE</small></span></div>
                <div className="agent-card-grid">{allAgents.map((agent, index) => <article className="agent-card" key={agent.id}><div className="agent-card-top"><span className="agent-avatar">{index < 4 ? <BotMessageSquare size={18}/> : <BrainCircuit size={18}/>}</span><span className={`agent-kind ${index < 4 ? 'builtin' : ''}`}>{index < 4 ? 'CORE SPECIALIST' : 'CUSTOM AGENT'}</span><button className="agent-delete" disabled={index < 4} title="Delete agent" onClick={() => { setCustomAgents(items => items.filter(item => item.id !== agent.id)); if (selectedAgent === agent.id) setSelectedAgent('default'); }}><Trash2 size={14}/></button></div><h3>{agent.name}</h3><p>{agent.specialty}</p><div className="agent-instructions">{agent.instructions}</div><div className="agent-card-foot"><span>{agent.model}</span><button onClick={() => { setSelectedAgent(agent.id); setActiveView('agent'); }}>Use agent <ArrowRight size={13}/></button></div></article>)}</div>
              </div>}

              {activeView === 'tools' && <div className="page-view studio-view">
                <div className="page-heading"><div><div className="eyebrow">CAPABILITIES / REGISTRY</div><h2>Tools & connections.</h2><p>Inspect Daemon's built-in tools and connected MCP servers. Trust and enable external servers in Daemon before using them.</p></div><button className="soft-button" onClick={loadToolCatalog}><RefreshCw size={14}/> Refresh registry</button></div>
                <div className="tools-overview-grid"><div className="tools-overview-card"><Wrench size={18}/><strong>Built-in tools</strong><span>Filesystem, shell, code intelligence and project utilities exposed by the Daemon runtime.</span></div><div className="tools-overview-card"><Globe2 size={18}/><strong>MCP connections</strong><span>External tool servers are managed through Daemon's trust and permission flow.</span></div><div className="tools-overview-card"><Shield size={18}/><strong>Permission broker</strong><span>Actions remain subject to the existing approval policy. No silent privilege escalation.</span></div></div>
                <div className="tool-output-panel"><div className="tool-output-head"><span><Terminal size={14}/> REGISTRY OUTPUT</span><span>{toolBusy ? 'REFRESHING' : 'LOCAL DAEMON'}</span></div><pre>{toolBusy ? 'Reading Daemon tool registry…' : toolOutput || 'Choose Refresh registry to inspect the tools and MCP servers available to this installation.'}</pre></div>
              </div>}

              {activeView === 'activity' && <div className="page-view"><div className="page-heading"><div><div className="eyebrow">SESSION TRACE</div><h2>Activity</h2><p>A readable record of what this desktop session has done.</p></div><button className="soft-button" onClick={() => setActivities([])}>Clear log</button></div><div className="activity-page-list">{activities.length ? activities.map(item => <ActivityRow key={item.id} item={item}/>) : <div className="empty-panel"><Activity size={23}/><strong>No activity yet</strong><span>Workspace and run events will appear here.</span></div>}</div></div>}

              {activeView === 'settings' && <div className="page-view"><div className="page-heading"><div><div className="eyebrow">PREFERENCES</div><h2>Settings</h2><p>Desktop shell and Daemon runtime connection.</p></div></div><div className="settings-list"><div className="setting-row"><div className="setting-icon"><Bot size={17}/></div><div><strong>Daemon runtime</strong><span>{daemon.installed ? daemon.version : daemon.detail || 'Not found on PATH'}</span></div><button className="soft-button" onClick={() => invoke<DaemonStatus>('daemon_status').then(setDaemon)}>Recheck</button></div><div className="setting-row"><div className="setting-icon"><FolderOpen size={17}/></div><div><strong>Current workspace</strong><span>{workspace || 'No project selected'}</span></div><button className="soft-button" onClick={chooseWorkspace}>Change</button></div><div className="setting-row"><div className="setting-icon"><Shield size={17}/></div><div><strong>Execution permissions</strong><span>Managed by the existing Daemon permission broker.</span></div><span className="read-only-tag">INHERITED</span></div><div className="setting-row"><div className="setting-icon"><Terminal size={17}/></div><div><strong>Agent runtime</strong><span>Uses the installed Daemon engine; API credentials remain in Daemon's secure vault.</span></div><span className="read-only-tag">LOCAL</span></div></div><div className="setup-note"><Info size={16}/><div><strong>Arch Linux setup</strong><code>sudo pacman -S --needed webkit2gtk-4.1 base-devel curl wget file openssl appmenu-gtk-module libappindicator-gtk3 librsvg xdotool rustup nodejs npm python-pipx</code><code>rustup default stable</code><code>pipx install ../dist/daemon_agent-1.0.0-py3-none-any.whl</code><code>daemon auth add</code><code>npm install && npm run tauri dev</code><span>The desktop app remembers your last selected workspace and connects to the Daemon engine.</span></div></div></div>}
            </section>

            {inspectorOpen && <aside className="right-inspector">
              <div className="inspector-head"><span>INSPECTOR</span><button className="icon-button tiny" onClick={() => setInspectorOpen(false)}><X size={14}/></button></div>
              <div className="inspector-tabs"><button className={rightTab === 'context' ? 'active' : ''} onClick={() => setRightTab('context')}>Context</button><button className={rightTab === 'activity' ? 'active' : ''} onClick={() => setRightTab('activity')}>Activity <span>{activities.length || ''}</span></button></div>
              {rightTab === 'context' ? <div className="inspector-scroll">
                <div className="inspector-section"><div className="inspector-section-title">RUNTIME <span className="live-label"><i/>LIVE</span></div><div className="runtime-card"><div className="runtime-top"><div className="runtime-glyph"><Cpu size={17}/></div><div><strong>Daemon Core</strong><small>{daemon.installed ? 'Connected to Daemon' : 'Waiting for Daemon'}</small></div></div><div className="runtime-line"><span>Version</span><b>{daemon.installed ? '1.0.0' : '—'}</b></div><div className="runtime-line"><span>Execution</span><b>Local process</b></div><div className="runtime-line"><span>Mode</span><b>Permission-aware</b></div></div></div>
                <div className="inspector-section"><div className="inspector-section-title">WORKSPACE</div><div className="context-card"><div className="context-path"><Folder size={14}/><span>{workspace ? workspace.split(/[\\/]/).filter(Boolean).pop() : 'Not selected'}</span></div><div className="context-stat-grid"><div><strong>{entries.length}</strong><span>Items here</span></div><div><strong>{gitChanges}</strong><span>Changes</span></div></div><div className="branch-line"><GitBranch size={13}/><span>{gitBranch}</span></div><button className="text-action" onClick={() => workspace && invoke('reveal_workspace', { path: workspace }).catch(e => setNotice(String(e)))}>Reveal in file manager <ArrowRight size={13}/></button></div></div>
                <div className="inspector-section"><div className="inspector-section-title">QUICK COMMANDS</div><div className="quick-commands"><button onClick={() => runUtility('doctor', 'Daemon Doctor')}><Gauge size={14}/><span>Doctor</span><ArrowRight size={12}/></button><button onClick={() => runUtility('models', 'Available Models')}><Layers3 size={14}/><span>Models</span><ArrowRight size={12}/></button><button onClick={() => runUtility('auth', 'Authentication Profiles')}><LockKeyhole size={14}/><span>Auth profiles</span><ArrowRight size={12}/></button><button onClick={() => runUtility('sessions', 'Saved Sessions')}><History size={14}/><span>Sessions</span><ArrowRight size={12}/></button></div></div>
                <div className="inspector-note"><span className="note-mark">“</span><p>The agent does the work.<br/>You keep the steering wheel.</p><small>DAEMON PRINCIPLE 01</small></div>
              </div> : <div className="inspector-scroll activity-inspector">{activities.length ? activities.slice(0, 10).map(item => <ActivityRow key={item.id} item={item}/>) : <div className="quiet-empty">No recent events.</div>}</div>}
              <div className="inspector-foot"><span><span className={`status-dot ${daemon.installed ? 'green' : 'amber'}`}/>{daemon.installed ? 'DAEMON READY' : 'RUNTIME NOT DETECTED'}</span><span>1.0.0</span></div>
            </aside>}
          </div>
        </main>
      </div>

      {agentModal && <div className="modal-scrim" onMouseDown={e => { if (e.target === e.currentTarget) setAgentModal(false); }}><div className="agent-modal"><div className="utility-modal-head"><div><span className="eyebrow">AGENT STUDIO / NEW SPECIALIST</span><h3>Define a new agent.</h3></div><button className="icon-button" onClick={() => setAgentModal(false)}><X size={16}/></button></div><div className="agent-form"><label>Agent name<input autoFocus placeholder="e.g. Performance Engineer" value={agentDraft.name} onChange={e => setAgentDraft(d => ({ ...d, name: e.target.value }))}/></label><label>Specialty<input placeholder="e.g. profiling, optimization, benchmarks" value={agentDraft.specialty} onChange={e => setAgentDraft(d => ({ ...d, specialty: e.target.value }))}/></label><label>Model preference<input placeholder="Use active Daemon model" value={agentDraft.model} onChange={e => setAgentDraft(d => ({ ...d, model: e.target.value }))}/><small>Optional model ID override for Build and Plan. Leave blank to use Daemon’s active model; Research uses its configured research model.</small></label><label>Agent brief / system instructions<textarea rows={7} placeholder="Describe the agent's role, working method, boundaries, and expected output…" value={agentDraft.instructions} onChange={e => setAgentDraft(d => ({ ...d, instructions: e.target.value }))}/></label></div><div className="utility-modal-foot"><span>Stored locally on this device</span><div className="agent-modal-actions"><button className="soft-button" disabled={generatingAgent || !agentDraft.name.trim() || !agentDraft.specialty.trim()} onClick={generateAgentInstructions}>{generatingAgent ? <LoaderCircle size={13} className="spin"/> : <WandSparkles size={13}/>} {generatingAgent ? "Generating…" : "Generate with API"}</button><button className="primary-button" disabled={!agentDraft.name.trim() || !agentDraft.instructions.trim()} onClick={() => { const agent: CustomAgent = { id: `custom-${Date.now()}`, name: agentDraft.name.trim(), specialty: agentDraft.specialty.trim() || 'General specialist', model: agentDraft.model.trim() || 'Default provider', instructions: agentDraft.instructions.trim(), createdAt: new Date().toISOString() }; setCustomAgents(items => [...items, agent]); setSelectedAgent(agent.id); setAgentModal(false); addActivity('Agent created', agent.name, 'green'); setNotice(`${agent.name} is ready. Select it in the composer to use it.`); }}><Save size={14}/> Save agent</button></div></div></div></div>}
      {searchOpen && <div className="search-overlay" onMouseDown={e => { if (e.target === e.currentTarget) setSearchOpen(false); }}><div className="search-modal"><div className="search-modal-top"><Search size={17}/><input autoFocus placeholder="Filter workspace files…" value={searchTerm} onChange={e => setSearchTerm(e.target.value)}/><button onClick={() => { setSearchOpen(false); setSearchTerm(''); }}><kbd>ESC</kbd></button></div><div className="search-results">{filteredEntries.slice(0, 12).map(entry => <button key={entry.path} onClick={() => { openFile(entry); setSearchOpen(false); }}><FileText size={14}/><span>{entry.name}</span><small>{entry.kind}</small></button>)}</div></div></div>}
      {utility && <div className="modal-scrim" onMouseDown={e => { if (e.target === e.currentTarget && !utilityBusy) setUtility(null); }}><div className="utility-modal"><div className="utility-modal-head"><div><span className="eyebrow">DAEMON OUTPUT</span><h3>{utility.title}</h3></div><button className="icon-button" onClick={() => setUtility(null)} disabled={utilityBusy}><X size={16}/></button></div><pre>{utility.output}</pre><div className="utility-modal-foot"><span>{utilityBusy ? 'Command running…' : 'Completed locally'}</span><button className="soft-button" onClick={() => setUtility(null)}>Close</button></div></div></div>}
      {notice && <div className="toast" onClick={() => setNotice('')}><Info size={15}/>{notice}<button><X size={13}/></button></div>}
    </div>
  );
}

function ArrowUpRightIcon() { return <ArrowRight size={14} className="starter-arrow"/>; }
function ActivityRow({ item }: { item: ActivityItem }) {
  return <div className="activity-row"><span className={`activity-marker ${item.tone || ''}`}/><div><strong>{item.label}</strong><small>{item.detail}</small></div><time>{item.time}</time></div>;
}

export default App;
