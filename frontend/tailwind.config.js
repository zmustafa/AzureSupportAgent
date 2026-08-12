/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        brand: {
          DEFAULT: "#1f6feb",
          dark: "#0d2a4a",
        },
      },
      keyframes: {
        // A bar that sweeps left→right to show active, ongoing work (indeterminate progress).
        indeterminate: {
          "0%": { left: "-45%", width: "45%" },
          "50%": { left: "35%", width: "55%" },
          "100%": { left: "100%", width: "45%" },
        },
        // A bright opacity + glow blink, so the status bar visibly pulses while running.
        blinkBright: {
          "0%, 100%": { opacity: "1", filter: "brightness(1.5)" },
          "50%": { opacity: "0.55", filter: "brightness(1)" },
        },
        cardShimmer: {
          "0%": { transform: "translateX(-160%)" },
          "100%": { transform: "translateX(360%)" },
        },
      },
      animation: {
        indeterminate: "indeterminate 1.15s ease-in-out infinite",
        "blink-bright": "blinkBright 0.9s ease-in-out infinite",
        "card-shimmer": "cardShimmer 1.4s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};
