import { createRoot } from "react-dom/client";
import { setAuthTokenGetter } from "@workspace/api-client-react";
import App from "./App";
import "./index.css";

const apiToken = (import.meta.env.VITE_AXIOM_API_TOKEN ?? "").trim();
if (apiToken) {
  setAuthTokenGetter(() => apiToken);
}

createRoot(document.getElementById("root")!).render(<App />);
