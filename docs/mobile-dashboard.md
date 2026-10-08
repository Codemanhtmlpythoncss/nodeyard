# Mobile dashboard refinements

The desktop sidebar and desktop controls remain in place above the mobile breakpoint. On phones, the dashboard uses a compact bottom navigation and a keyboard-accessible **More** sheet. These 50 refinements make the existing dashboard sections usable on small screens without removing their desktop behavior.

## Navigation and controls

1. Fit the page to iPhone safe areas with `viewport-fit=cover`.
2. Keep content clear of the top camera/notch inset.
3. Keep the bottom tabs clear of the home indicator.
4. Put Overview in the primary tabs.
5. Put Nodes in the primary tabs.
6. Put Pods in the primary tabs.
7. Put AI in the primary tabs.
8. Put every other dashboard section in More.
9. Give primary sections compact icons and readable labels.
10. Mark the current section visibly and with `aria-current`.
11. Show available node, pod, workload, and alert counts in navigation.
12. Keep all section links in the More sheet, including their current-page state.
13. Use a labelled dialog for the More sheet.
14. Add a close button to the sheet.
15. Close the sheet when its backdrop is tapped.
16. Close it with Escape.
17. Move keyboard focus into the sheet when it opens.
18. Keep Tab and Shift+Tab inside the open sheet.
19. Return focus to the opener when the sheet closes.
20. Mark the sheet modal and hide it from assistive technology while closed.
21. Make the rest of the dashboard inert while the sheet is open.
22. Lock page scrolling while the sheet is open.
23. Contain scrolling inside the sheet.
24. Close the sheet after a section is chosen.
25. Move focus to the selected page content after navigation.
26. Close the sheet when the viewport returns to desktop width.
27. Restore focus to the active desktop navigation link on that resize.
28. Put theme switching in the More sheet.
29. Put snapshot export in the More sheet.
30. Put live-update control in the More sheet.
31. Put refresh-now in the More sheet.
32. Put refresh interval selection in the More sheet.
33. Put keyboard-shortcut help in the More sheet.
34. Show sign-out there only when the server says a session is signed in.
35. Keep the live state and refresh interval synchronized with desktop controls.
36. Keep a one-tap refresh button in the phone header.

## Layout, touch, and accessibility

37. Reduce the header height and title size on phone screens.
38. Keep the cluster status visible as a compact dot with an announced text status.
39. Hide snapshot export from the phone header when the More sheet provides the same action.
40. Reserve content space above the fixed bottom tabs.
41. Use two columns for overview KPI cards on narrow screens.
42. Tighten metric rows while preserving readable values.
43. Let toolbar controls wrap instead of overflowing the viewport.
44. Give buttons, fields, and selects phone-friendly minimum heights.
45. Keep small buttons usable without making them as large as primary actions.
46. Allow tables to scroll horizontally with touch momentum.
47. Use a full-height details drawer on phones with safe-area padding.
48. Keep dialogs within the visible safe area and allow their bodies to scroll.
49. Keep confirmations and chat sizing within the dynamic viewport height.
50. Use 16px mobile form text to prevent iOS from zooming when fields are focused.

## Background work

The live stream and polling stop while the page is hidden. On return, the dashboard reconnects its stream, requests a fresh snapshot, and resumes the selected refresh interval. This avoids spending requests and redraw time on a background tab.
