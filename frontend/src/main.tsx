import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { AuthProvider } from "./components/AuthContext";
import { ConfirmProvider } from "./components/ConfirmDialog";
import { UpdateBanner } from "./components/UpdateBanner";
import { queryClient } from "./queryClient";
import { initWebVitals } from "./webVitals";
import { setupPWA } from "./pwa";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <ConfirmProvider>
            <App />
            <UpdateBanner />
          </ConfirmProvider>
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);

// Report LCP / INP / CLS / TTFB / FCP. Console-only by default; opt-in via
// localStorage.setItem("azsup.vitals","1") in dev.
initWebVitals();

// Register the service worker, poll for new deploys, and prompt to reload (no-op in dev —
// the SW is only emitted in production builds).
setupPWA();
