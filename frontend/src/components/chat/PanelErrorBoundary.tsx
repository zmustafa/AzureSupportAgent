import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  /** Human-readable area name, shown in the fallback (e.g. "Monitor"). */
  name?: string;
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/** Catches render-time errors in a lazily-loaded panel so one broken view can't
 *  white-screen the whole app. Offers a one-click recovery (reset + reload of the
 *  subtree) without a full page refresh. */
export class PanelErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Surface to the console for diagnostics; the UI stays usable.
    console.error(`Panel "${this.props.name ?? "view"}" crashed:`, error, info);
  }

  private reset = () => this.setState({ error: null });

  render() {
    if (this.state.error) {
      return (
        <div className="flex h-full items-center justify-center p-8">
          <div className="max-w-md rounded-xl border border-red-200 bg-red-50 p-6 text-center">
            <div className="mb-1 text-sm font-semibold text-red-700">
              The {this.props.name ?? "view"} hit an unexpected error
            </div>
            <p className="mb-4 text-xs text-red-600">
              The rest of the app is still working. You can retry this view, or switch to
              another section from the sidebar.
            </p>
            <pre className="mb-4 max-h-32 overflow-auto rounded-md bg-white/70 px-3 py-2 text-left font-mono text-[11px] text-red-500">
              {this.state.error.message || String(this.state.error)}
            </pre>
            <button
              onClick={this.reset}
              className="rounded-md bg-red-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-red-700"
            >
              Retry
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
