import { cn } from "@/lib/utils";

export function Input({ className, ...props }: React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={cn(
        "flex h-11 w-full rounded-full border border-white/[0.10] bg-white/[0.065] px-4 py-1 font-sans text-[0.92rem] tracking-[-0.018em] text-midground shadow-[inset_0_1px_0_rgba(255,255,255,.06)] backdrop-blur-xl transition-all",
        "placeholder:text-muted-foreground/70",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#009dff]/45 focus-visible:border-[#009dff]/45 focus-visible:bg-white/[0.095]",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...props}
    />
  );
}
