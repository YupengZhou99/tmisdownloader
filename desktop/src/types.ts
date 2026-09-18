export type Task = {
  id: string; batch_id: string; kind: string; source_name: string; output_name: string;
  output_dir: string; status: string; stage: string; error: string; saved_path: string;
  attempt_count: number; treasury: string; start: string; end: string; row_number: number;
};
export type Batch = { id: string; source_name: string; output_dir: string; created: string; options: string | object; total?: number; succeeded?: number };
export type Log = { text: string; level: string; time?: string; log_id?: string };
export type State = {
  tasks: Task[]; batches: Batch[]; counts: Record<string, number>; mode: string; current: string | null;
  session_ready: boolean; headless: boolean; pending_headless: boolean | null; switching: boolean;
  importing: boolean; state_dir: string; version: string; logs?: Log[]; top?: boolean;
  task_total?: number; current_task?: Task | null;
  resources?: { parked?: boolean; browser_tree_rss?: number }; resource_hold?: boolean; run_requested?: boolean;
};
export type Preview = {
  id?: string; name: string; count?: number; start?: string; end?: string;
  kinds?: Record<string, number>; duplicate?: boolean; duplicate_workspaces?: string[]; error?: string;
  sample?: { kind: string; output_name: string; params: Record<string, string> }[];
};
export type Options = { start_date: string; end_date: string; naming_mode: string; postprocess: boolean };
export type WorkspaceInfo = { id: string; name: string; root: string; folder: string; browserPath: string; headless?: boolean;
  archived: boolean; online: boolean; confirmed: boolean; error: string; busy: string | null; worker_pid?: number | null; state: State; logs: Log[] };
export type WorkspaceSnapshot = { version: string; limit: number; workspaces: WorkspaceInfo[]; closing: boolean; top?: boolean };
declare global {
  interface Window {
    tmis?: {
      call: <T = unknown>(command: string, data?: object, workspaceId?: string) => Promise<T>;
      workspaces: <T = unknown>(action: string, data?: object) => Promise<T>;
      files: (workspaceId?: string) => Promise<string[]>;
      directory: (workspaceId?: string) => Promise<string>;
      browser: () => Promise<string>;
      drop: (files: File[], workspaceId?: string) => Promise<string[]>;
      window: (action: string, value?: boolean | string) => Promise<void>;
      open: (path: string, workspaceId?: string) => Promise<void>;
      subscribe: (callback: (data: Record<string, any>) => void) => () => void;
    };
  }
}
