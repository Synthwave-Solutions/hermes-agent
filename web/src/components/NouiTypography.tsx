import { forwardRef, type ElementType, type HTMLAttributes, type ReactNode } from "react";
import { cn } from "@/lib/utils";

type TypographyProps = HTMLAttributes<HTMLElement> & {
  as?: ElementType;
  children?: ReactNode;
  compressed?: boolean;
  courier?: boolean;
  expanded?: boolean;
  mondwest?: boolean;
  mono?: boolean;
  sans?: boolean;
  variant?: "sm" | "md" | "lg" | "xl";
};

const variantClasses: Record<NonNullable<TypographyProps["variant"]>, string> = {
  sm: "text-[0.86rem] leading-[1.38] tracking-[-0.012em]",
  md: "text-[1.75rem] leading-[1.08] tracking-[-0.035em]",
  lg: "text-[2.28rem] leading-[1.06] tracking-[-0.045em]",
  xl: "text-[3.5rem] leading-[1.03] tracking-[-0.055em]",
};

export const Typography = forwardRef<HTMLElement, TypographyProps>(function Typography(
  {
    as: Component = "span",
    className,
    compressed,
    courier,
    expanded,
    mondwest,
    mono,
    sans,
    variant,
    ...props
  },
  ref,
) {
  const hasFontVariant = compressed || courier || expanded || mondwest || mono || sans;

  return (
    <Component
      className={cn(
        compressed && "font-compressed",
        courier && "font-courier",
        expanded && "font-expanded",
        mondwest && "font-sans tracking-[-0.018em]",
        mono && "font-mono",
        (!hasFontVariant || sans) && "font-sans",
        variant && variantClasses[variant],
        className,
      )}
      ref={ref}
      {...props}
    />
  );
});

export const H2 = forwardRef<HTMLHeadingElement, Omit<TypographyProps, "as">>(function H2(
  { className, variant = "lg", ...props },
  ref,
) {
  return <Typography as="h2" className={cn("font-bold", className)} variant={variant} ref={ref} {...props} />;
});
