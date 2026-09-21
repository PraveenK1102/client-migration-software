import { useEffect, useState } from "react";

export type Theme = "light" | "dark";
const KEY = "dbx.theme";

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      const t = localStorage.getItem(KEY);
      if (t === "light" || t === "dark") return t;
    } catch { /* ignore */ }
    return "light"; // light is the default
  });
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem(KEY, theme); } catch { /* ignore */ }
  }, [theme]);
  return { theme, toggle: () => setTheme((t) => (t === "light" ? "dark" : "light")) };
}
