export type Task = {
  id: string; batch_id: string; kind: string; source_name: string; output_name: string;
  output_dir: string; status: string; stage: string; error: string; saved_path: string;
  attempt_count: number; treasury: string; start: string; end: string; row_number: number;
};
export type Batch = { id: string; source_name: string; output_dir: string; created: string; options: string | object };
export type Log = { text: string; level: string; time?: string };
export type State = {
  tasks: Task[]; batches: Batch[]; counts: Record<string, number>; mode: string; current: string | null;
  session_ready: boolean; headless: boolean; pending_headless: boolean | null; switching: boolean;
  importing: boolean; state_dir: string; version: string; logs?: Log[]; top?: boolean;
};
export type Preview = {
  id?: string; name: string; count?: number; start?: string; end?: string;
  kinds?: Record<string, number>; duplicate?: boolean; error?: string;
  sample?: { kind: string; output_name: string; params: Record<string, string> }[];
};
export type Options = { start_date: string; end_date: string; naming_mode: string; postprocess: boolean };
declare global {
  interface Window {
    tmis?: {
      call: <T = unknown>(command: string, data?: object) => Promise<T>;
      files: () => Promise<string[]>;
      directory: () => Promise<string>;
      browser: () => Promise<string>;
      drop: (files: File[]) => Promise<string[]>;
      window: (action: string, value?: boolean) => Promise<void>;
      open: (path: string) => Promise<void>;
      subscribe: (callback: (data: Record<string, any>) => void) => () => void;
    };
  }
}
