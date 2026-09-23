import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props { children: ReactNode }
interface State { hasError: boolean }

export class AppErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false };

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Keep output safe: do not emit error objects or component stacks.
    void error;
    void info;
  }

  render() {
    if (!this.state.hasError) return this.props.children;
    return <main className="error-boundary" role="alert"><div className="safety-note"><strong>REAL-MONEY EXECUTION: DISABLED</strong><h1>แดชบอร์ดแสดงผลไม่ได้ชั่วคราว</h1><p>ข้อมูลบางส่วนจาก Research หรือ Observability ไม่สมบูรณ์ ระบบยังคงเป็น read-only และไม่มีการส่งคำสั่งซื้อขาย</p><div className="inline-actions"><button className="text-button" onClick={() => window.location.reload()}>ลองโหลดใหม่</button><button className="text-button" onClick={() => window.history.back()}>ย้อนกลับ</button></div></div></main>;
  }
}
