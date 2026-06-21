# Design Style Guide - Student Attendance System

Tài liệu này tóm tắt phong cách thiết kế hiện tại của dự án để có thể áp dụng cho một dự án khác mà không cần sao chép nguyên mã nguồn.

## 1. Tinh thần thiết kế

Phong cách tổng thể là dashboard quản trị học vụ: rõ ràng, thực dụng, sáng màu, nhiều khoảng trắng vừa đủ, ưu tiên tốc độ thao tác hơn yếu tố trang trí.

Giao diện nên tạo cảm giác:

- Sạch, hiện đại, dễ đọc.
- Phù hợp hệ thống quản lý lớp học, sinh viên, điểm danh, bài thi.
- Ít trang trí, tập trung vào thẻ thông tin, bảng dữ liệu, form và hành động nhanh.
- Có phản hồi trạng thái rõ ràng: loading, success, error, empty state.

Không nên đi theo hướng landing page, hero marketing, gradient nặng, animation phức tạp hoặc giao diện quá minh họa.

## 2. Công nghệ và thư viện UI

Dự án dùng:

- React.
- Tailwind CSS.
- `lucide-react` cho icon.
- Component tự xây dựng: `Button`, `Card`, `Input`, `Modal`, `Table`.

Khi áp dụng cho dự án khác, nên giữ cách tiếp cận component nhỏ, tái sử dụng được, thay vì dùng UI framework quá nặng.

## 3. Bảng màu

### Màu nền chính

- Nền app: `#f8fafc` hoặc Tailwind `bg-gray-50`.
- Nền card, sidebar, header: `bg-white`.
- Viền nhẹ: `border-gray-200`, `border-gray-300`.

### Màu chữ

- Tiêu đề chính: `text-gray-900`.
- Nội dung phụ: `text-gray-600`.
- Metadata, ghi chú nhỏ: `text-gray-500`.
- Placeholder/icon phụ: `text-gray-400`.

### Màu thương hiệu chính

Dùng xanh dương sky làm màu primary:

```js
primary: {
  50: '#f0f9ff',
  100: '#e0f2fe',
  200: '#bae6fd',
  300: '#7dd3fc',
  400: '#38bdf8',
  500: '#0ea5e9',
  600: '#0284c7',
  700: '#0369a1',
  800: '#075985',
  900: '#0c4a6e',
}
```

Cách dùng:

- Nút chính: `bg-primary-600`, hover `bg-primary-700`.
- Focus ring: `focus:ring-primary-500`.
- Link: `text-primary-600`, hover `text-primary-700`.
- Sidebar active: `bg-primary-50 text-primary-700`.
- Icon avatar/profile: `bg-primary-100 text-primary-600`.

### Màu phụ

Dự án có secondary tím, dùng nhẹ hơn primary:

```js
secondary: {
  50: '#faf5ff',
  100: '#f3e8ff',
  200: '#e9d5ff',
  300: '#d8b4fe',
  400: '#c084fc',
  500: '#a855f7',
  600: '#9333ea',
  700: '#7e22ce',
  800: '#6b21a8',
  900: '#581c87',
}
```

### Màu trạng thái

- Success: `green-50`, `green-100`, `green-600`, `green-700`.
- Danger/error: `red-50`, `red-100`, `red-600`, `red-700`.
- Warning/medium: `yellow-100`, `yellow-500`, `yellow-600`, `yellow-800`.
- Informational: `blue-50`, `blue-100`, `blue-500`, `blue-600`, `blue-800`.
- Accent dashboard: `blue-500`, `green-500`, `purple-500`, `orange-500`.

## 4. Typography

Font stack:

```css
system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif
```

Quy ước chữ:

- Page title: `text-2xl` hoặc `text-3xl`, `font-bold`, `text-gray-900`.
- Card title: `text-lg`, `font-semibold`, `text-gray-900`.
- Modal title: `text-xl`, `font-semibold`.
- Label form: `text-sm`, `font-medium`, `text-gray-700`.
- Body text: `text-sm` hoặc `text-base`, `text-gray-600`.
- Số liệu dashboard: `text-3xl` hoặc `text-4xl`, `font-bold`, `text-gray-900`.
- Table header: `text-xs`, `font-medium`, uppercase, `tracking-wider`, `text-gray-500`.

## 5. Layout tổng thể

### App shell

Các màn hình chính dùng layout dashboard:

- Nền toàn trang: `min-h-screen bg-gray-50`.
- Sidebar cố định bên trái trên desktop.
- Sidebar mobile trượt vào với backdrop xám.
- Nội dung chính dịch theo độ rộng sidebar:
  - Sidebar mở: `lg:pl-64`.
  - Sidebar thu gọn: `lg:pl-16`.
- Sidebar rộng `w-64`, khi collapsed là `w-16`.

### Header trang

Header của trang thường là:

```html
<header className="bg-white shadow-sm border-b">
  <div className="max-w-7xl mx-auto px-4 py-6">
    ...
  </div>
</header>
```

Biến thể nhỏ hơn dùng `py-4`.

Header nên có:

- Tiêu đề trang.
- Mô tả ngắn bên dưới.
- Nút hành động chính ở góc phải nếu cần.

### Content container

Nội dung chính:

```html
<main className="max-w-7xl mx-auto px-4 py-8">
  ...
</main>
```

Grid phổ biến:

- Stats dashboard: `grid grid-cols-1 md:grid-cols-3 gap-6`.
- Admin stats: `grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6`.
- Danh sách card: `grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6`.
- Form/filter ngắn: `grid grid-cols-1 md:grid-cols-3 gap-4`.

## 6. Component chính

### Button

Nút có phong cách:

```txt
inline-flex items-center justify-center gap-2 font-medium rounded-lg
transition-all duration-200 focus:outline-none focus:ring-2 focus:ring-offset-2
disabled:opacity-50 disabled:cursor-not-allowed
```

Kích thước:

- Small: `px-3 py-1.5 text-sm`.
- Medium: `px-4 py-2 text-base`.
- Large: `px-6 py-3 text-lg`.

Variants:

- Primary: `bg-primary-600 text-white hover:bg-primary-700`.
- Secondary: `bg-gray-200 text-gray-800 hover:bg-gray-300`.
- Success: `bg-green-600 text-white hover:bg-green-700`.
- Danger: `bg-red-600 text-white hover:bg-red-700`.
- Warning: `bg-yellow-500 text-white hover:bg-yellow-600`.
- Outline: `border-2 border-primary-600 text-primary-600 hover:bg-primary-50`.
- Ghost: `text-gray-700 hover:bg-gray-100`.
- Link: `text-primary-600 hover:text-primary-700 hover:underline`.

Nút thường đi kèm icon Lucide kích thước `w-4 h-4` hoặc `w-5 h-5`.

### Card

Card là đơn vị thị giác chính:

```txt
bg-white rounded-lg shadow-md p-6
```

Biến thể:

- Padding nhỏ: `p-4`.
- Padding lớn: `p-8`.
- Hover card: `hover:shadow-lg transition-shadow`.

Card dùng cho:

- Stats.
- Quick actions.
- Danh sách lớp/môn/bài thi/câu hỏi.
- Empty state.
- Khối thông tin profile.

Không nên lồng quá nhiều card trong card. Nếu cần nhóm nhỏ bên trong, dùng `bg-gray-50 rounded-lg p-3/p-4`.

### Input

Input chuẩn:

```txt
w-full px-4 py-2 border border-gray-300 rounded-lg
focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent
disabled:bg-gray-100 disabled:cursor-not-allowed
```

Label:

```txt
block text-sm font-medium text-gray-700 mb-1
```

Icon trong input:

- Đặt absolute bên trái: `left-3 top-1/2 -translate-y-1/2`.
- Input có icon dùng `pl-10`.
- Icon màu `text-gray-400`.

Lỗi form:

- Border đỏ: `border-red-500`.
- Message: `mt-1 text-sm text-red-600`.

### Modal

Modal:

- Overlay: `fixed inset-0 bg-black bg-opacity-50 p-4`.
- Container: `bg-white rounded-lg shadow-xl w-full max-h-[90vh] flex flex-col`.
- Header: `px-6 py-4 border-b`.
- Content: `px-6 py-4 overflow-y-auto flex-1`.
- Footer: `px-6 py-4 border-t bg-gray-50 rounded-b-lg`.

Kích thước:

- `sm`: `max-w-md`.
- `md`: `max-w-lg`.
- `lg`: `max-w-2xl`.
- `xl`: `max-w-4xl`.

### Table

Bảng tối giản, dễ scan:

- Wrapper: `overflow-x-auto`.
- Table: `min-w-full divide-y divide-gray-200`.
- Header: `bg-gray-50`.
- Header cell: `px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider`.
- Body: `bg-white divide-y divide-gray-200`.
- Cell: `px-6 py-4 whitespace-nowrap text-sm text-gray-900`.
- Hover row nếu click được: `cursor-pointer hover:bg-gray-50 transition-colors`.
- Empty row: `px-6 py-12 text-center text-gray-500`.

## 7. Sidebar và navigation

Sidebar:

- Nền: `bg-white`.
- Viền phải: `border-r border-gray-200`.
- Fixed full height: `fixed inset-y-0 left-0`.
- Transition: `transition-all duration-200 ease-in-out`.
- Mobile ẩn/hiện bằng translate.

Navigation item:

```txt
w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium transition-colors
```

Trạng thái:

- Active: `bg-primary-50 text-primary-700`.
- Default: `text-gray-700 hover:bg-gray-100 hover:text-gray-900`.
- Disabled: `text-gray-400 cursor-not-allowed`.
- Logout: `text-red-700 hover:bg-red-50`.

Icon nav:

- Lucide icon `w-5 h-5`.
- Khi collapsed chỉ hiển thị icon, dùng `title` làm tooltip mặc định.

## 8. Iconography

Dự án dùng icon từ `lucide-react`.

Quy ước:

- Icon trong button: `w-4 h-4` hoặc `w-5 h-5`.
- Icon trong stats card: `w-8 h-8 text-white`, nằm trong hộp màu `p-3` hoặc `p-4 rounded-lg`.
- Icon empty state: `w-12 h-12` hoặc `w-16 h-16`, `text-gray-400`.
- Icon avatar/profile: nằm trong vòng tròn `rounded-full bg-primary-100`.

Các icon thường dùng:

- Dashboard: `LayoutDashboard`.
- Lớp học/môn học: `BookOpen`, `FileText`.
- Người dùng: `User`, `Users`.
- Đăng xuất: `LogOut`.
- Menu mobile: `Menu`, `X`.
- Thao tác: `Plus`, `Eye`, `Trash2`, `ArrowRight`.
- QR/attendance: `QrCode`, `ClipboardCheck`, `CheckCircle`.

## 9. Cards dashboard và quick actions

### Stats card

Cấu trúc phổ biến:

```html
<Card className="hover:shadow-lg transition-shadow">
  <div className="flex items-center justify-between">
    <div>
      <p className="text-gray-600 text-sm mb-1">Label</p>
      <p className="text-4xl font-bold text-gray-900 mb-1">123</p>
      <p className="text-sm text-gray-500">Mô tả</p>
    </div>
    <div className="bg-blue-500 p-4 rounded-lg">
      <Icon className="w-8 h-8 text-white" />
    </div>
  </div>
</Card>
```

Màu icon stats nên xoay vòng:

- Blue: lớp học, tổng số.
- Green: sinh viên, thành công.
- Purple: điểm danh/bài thi.
- Orange: báo cáo/đăng ký/thống kê phụ.

### Quick action

Quick action là button dạng card nhỏ:

```txt
text-left p-4 border-2 border-gray-200 rounded-lg
hover:border-primary-500 hover:shadow-md transition-all group
```

Icon trong quick action:

```txt
w-12 h-12 rounded-lg flex items-center justify-center mb-3
group-hover:scale-110 transition-transform
```

Title hover đổi màu primary:

```txt
font-semibold text-gray-900 group-hover:text-primary-600 transition-colors
```

## 10. Badge và trạng thái

Badge nên nhỏ, bo tròn nhiều:

```txt
px-2 py-0.5 rounded-full text-xs font-medium
px-3 py-1 rounded-full text-sm font-medium
```

Ví dụ:

- Số sinh viên: `bg-blue-100 text-blue-800`.
- Dễ: `bg-green-100 text-green-800`.
- Trung bình: `bg-yellow-100 text-yellow-800`.
- Khó/lỗi: `bg-red-100 text-red-800`.
- Disabled/neutral: `bg-gray-100 text-gray-700`.

Alert:

- Error: `p-4 bg-red-50 border border-red-200 rounded-lg text-red-700`.
- Success: `p-4 bg-green-50 border border-green-200 rounded-lg text-green-700`.
- Info: `p-4 bg-blue-50 border border-blue-200 rounded-lg text-blue-800`.

## 11. Empty, loading và feedback

Loading spinner:

```html
<div className="animate-spin rounded-full h-12 w-12 border-b-2 border-primary-600 mx-auto" />
```

Loading container:

```txt
text-center py-8
text-center py-12
```

Empty state:

- Đặt trong card hoặc khối `bg-gray-50 rounded-lg`.
- Canh giữa: `text-center py-8` hoặc `py-12`.
- Icon xám: `text-gray-400`.
- Title: `text-lg font-semibold text-gray-900`.
- Description: `text-gray-600`.
- Có nút primary nếu empty state có hành động tiếp theo.

## 12. Form pattern

Form dùng spacing:

```txt
space-y-4
```

Footer action trong form/modal:

```txt
flex gap-3 pt-4
```

Hai nút phổ biến:

- Cancel: `variant="outline"`.
- Submit: `variant="primary"`.
- Trong modal, thường dùng `fullWidth` cho cả hai nút.

Validation:

- Xóa lỗi khi người dùng nhập lại.
- Error message nằm ngay dưới input.
- Alert tổng quát nằm trên form.

## 13. Responsive behavior

Breakpoints chính:

- Mobile mặc định 1 cột.
- `md`: 2 hoặc 3 cột tùy nội dung.
- `lg`: sidebar cố định, grid mở rộng 3 hoặc 4 cột.

Mobile:

- Sidebar ẩn, mở bằng nút menu ở top bar.
- Top bar mobile: `sticky top-0 z-10 bg-white border-b border-gray-200`.
- Nội dung giữ `px-4`, không dùng padding quá lớn.

Desktop:

- Sidebar luôn hiện.
- Content có `lg:pl-64` hoặc `lg:pl-16`.
- Container chính giới hạn `max-w-7xl`.

## 14. Bo góc, bóng và chuyển động

Bo góc:

- Thành phần chính: `rounded-lg`.
- Badge: `rounded-full`.
- Avatar: `rounded-full`.

Bóng:

- Card mặc định: `shadow-md`.
- Header: `shadow-sm`.
- Modal: `shadow-xl`.
- Hover card: `hover:shadow-lg`.

Animation/transition:

- Chủ yếu dùng `transition-colors`, `transition-shadow`, `transition-all`.
- Duration phổ biến: `duration-200`.
- Quick action icon có thể scale nhẹ: `group-hover:scale-110`.

## 15. Mẫu Tailwind config nên dùng

```js
/** @type {import('tailwindcss').Config} */
export default {
  content: [
    './index.html',
    './src/**/*.{js,ts,jsx,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        primary: {
          50: '#f0f9ff',
          100: '#e0f2fe',
          200: '#bae6fd',
          300: '#7dd3fc',
          400: '#38bdf8',
          500: '#0ea5e9',
          600: '#0284c7',
          700: '#0369a1',
          800: '#075985',
          900: '#0c4a6e',
        },
        secondary: {
          50: '#faf5ff',
          100: '#f3e8ff',
          200: '#e9d5ff',
          300: '#d8b4fe',
          400: '#c084fc',
          500: '#a855f7',
          600: '#9333ea',
          700: '#7e22ce',
          800: '#6b21a8',
          900: '#581c87',
        },
      },
    },
  },
  plugins: [],
}
```

## 16. Mẫu CSS nền tảng

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

:root {
  font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
  line-height: 1.5;
  font-weight: 400;
  color-scheme: light;
  font-synthesis: none;
  text-rendering: optimizeLegibility;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

body {
  margin: 0;
  min-height: 100vh;
  background-color: #f8fafc;
}

#root {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}
```

## 17. Checklist khi thiết kế màn hình mới

- Dùng nền `bg-gray-50`, không dùng nền trắng toàn trang.
- Có header trắng với title, description và action chính nếu cần.
- Bọc nội dung bằng `max-w-7xl mx-auto px-4 py-8`.
- Dùng card trắng `rounded-lg shadow-md p-6`.
- Dùng primary sky blue cho hành động chính và focus state.
- Dùng icon Lucide nhất quán.
- Dùng grid responsive `grid-cols-1` trên mobile.
- Có loading spinner màu primary.
- Có empty state khi chưa có dữ liệu.
- Có alert success/error rõ ràng.
- Giữ text nhỏ, gọn, phù hợp dashboard.
- Tránh gradient, trang trí lớn, hero marketing hoặc animation phức tạp.

## 18. Từ khóa mô tả phong cách

Nếu cần mô tả ngắn cho designer hoặc AI khác:

> Modern light academic admin dashboard, Tailwind CSS, sky-blue primary color, white cards on soft gray background, compact sidebar navigation, Lucide icons, rounded-lg components, subtle shadows, clean forms and tables, clear success/error states, responsive grid layouts, practical student/class management interface.
