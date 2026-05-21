/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2026 Baldur Karlsson
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 ******************************************************************************/

// UEVR / Nsight-style automation subcommands. Implements `renderdoccmd
// index-capture` (M1: walk every event, emit JSONL tables) and
// `renderdoccmd state-at-event` (M1+M2: snapshot pipeline state with
// register-to-resource resolution).
//
// See docs/UEVR_NSIGHT_AUTOMATION_ROADMAP.md for the design context.

#include "renderdoccmd.h"

#include <stdio.h>
#include <string.h>
#include <sstream>
#include <fstream>
#include <map>
#include <ostream>
#include <set>
#include <string>
#include <vector>

// renderdoccmd.cpp defines a similar inline ostream<<(rdcstr) — duplicate it
// here so this translation unit can stream rdcstr without bringing extra
// dependencies. Static inline to avoid ODR collisions.
static inline std::ostream &operator<<(std::ostream &os, const rdcstr &s)
{
  return os << s.c_str();
}

// conv() helpers are defined externally in renderdoccmd.cpp. We declare them
// at file scope here so they can be linked (their public signatures don't
// collide with the static wstring conv() in renderdoccmd_win32.cpp).
extern rdcstr conv(const std::string &s);
extern std::string conv(const rdcstr &s);

// Include pipestate.inl to instantiate the PipeState member functions in this
// translation unit. renderdoccmd.cpp already includes renderdoc_tostr.inl so
// we don't include it here (avoid duplicate symbols).
#include <replay/pipestate.inl>

#ifdef _WIN32
#include <direct.h>
#define MKDIR(p) _mkdir(p)
#else
#include <sys/stat.h>
#define MKDIR(p) mkdir(p, 0755)
#endif

// ---------------------------------------------------------------------------
// Tiny streaming JSON writer
// ---------------------------------------------------------------------------

namespace autoj
{
class Writer
{
public:
  Writer(std::string *out) : m_Out(out), m_Pretty(false) {}
  Writer(std::ostream *out, bool pretty) : m_Stream(out), m_Pretty(pretty) {}

  void BeginObject() { Push('{'); }
  void EndObject() { Pop('}'); }
  void BeginArray() { Push('['); }
  void EndArray() { Pop(']'); }

  void Key(const char *k)
  {
    Sep();
    EmitString(k);
    Append(':');
    m_NeedSep = false;
    m_KeyJustEmitted = true;
  }

  void Null()
  {
    Sep();
    Append("null", 4);
    m_NeedSep = true;
    m_KeyJustEmitted = false;
  }
  void Bool(bool v)
  {
    Sep();
    if(v)
      Append("true", 4);
    else
      Append("false", 5);
    m_NeedSep = true;
    m_KeyJustEmitted = false;
  }
  void Int(int64_t v)
  {
    Sep();
    char buf[24];
    snprintf(buf, sizeof(buf), "%lld", (long long)v);
    Append(buf);
    m_NeedSep = true;
    m_KeyJustEmitted = false;
  }
  void UInt(uint64_t v)
  {
    Sep();
    char buf[24];
    snprintf(buf, sizeof(buf), "%llu", (unsigned long long)v);
    Append(buf);
    m_NeedSep = true;
    m_KeyJustEmitted = false;
  }
  void Double(double v)
  {
    Sep();
    char buf[40];
    snprintf(buf, sizeof(buf), "%.9g", v);
    Append(buf);
    m_NeedSep = true;
    m_KeyJustEmitted = false;
  }
  void String(const char *s)
  {
    Sep();
    EmitString(s ? s : "");
    m_NeedSep = true;
    m_KeyJustEmitted = false;
  }
  void String(const std::string &s) { String(s.c_str()); }
  void NewlineRecord()
  {
    // Used for JSONL: separator between top-level objects.
    Append('\n');
    m_NeedSep = false;
    m_KeyJustEmitted = false;
  }

  void Flush()
  {
    if(m_Stream)
      m_Stream->flush();
  }

private:
  void Push(char c)
  {
    Sep();
    Append(c);
    m_Stack.push_back(c);
    m_NeedSep = false;
    m_KeyJustEmitted = false;
  }
  void Pop(char close)
  {
    if(!m_Stack.empty())
      m_Stack.pop_back();
    Append(close);
    m_NeedSep = true;
    m_KeyJustEmitted = false;
  }
  void Sep()
  {
    if(m_NeedSep)
      Append(',');
  }
  void EmitString(const char *s)
  {
    Append('"');
    for(const char *p = s; *p; ++p)
    {
      unsigned char c = (unsigned char)*p;
      switch(c)
      {
        case '"': Append("\\\"", 2); break;
        case '\\': Append("\\\\", 2); break;
        case '\b': Append("\\b", 2); break;
        case '\f': Append("\\f", 2); break;
        case '\n': Append("\\n", 2); break;
        case '\r': Append("\\r", 2); break;
        case '\t': Append("\\t", 2); break;
        default:
          if(c < 0x20)
          {
            char buf[8];
            snprintf(buf, sizeof(buf), "\\u%04x", c);
            Append(buf);
          }
          else
          {
            Append((char)c);
          }
      }
    }
    Append('"');
  }
  void Append(char c)
  {
    if(m_Out)
      m_Out->push_back(c);
    else if(m_Stream)
      m_Stream->put(c);
  }
  void Append(const char *s, size_t n)
  {
    if(m_Out)
      m_Out->append(s, n);
    else if(m_Stream)
      m_Stream->write(s, (std::streamsize)n);
  }
  void Append(const char *s) { Append(s, strlen(s)); }

  std::string *m_Out = NULL;
  std::ostream *m_Stream = NULL;
  bool m_Pretty = false;
  bool m_NeedSep = false;
  bool m_KeyJustEmitted = false;
  std::vector<char> m_Stack;
};
}    // namespace autoj

// ---------------------------------------------------------------------------
// Minimal MD5 (public domain, Solar Designer / Alexander Peslyak).
// Kept inline to avoid linking against renderdoc's internal copy.
// ---------------------------------------------------------------------------

namespace automd5
{
typedef unsigned int u32;
struct Ctx
{
  u32 lo, hi;
  u32 a, b, c, d;
  unsigned char buffer[64];
  u32 block[16];
};

#define F(x, y, z) ((z) ^ ((x) & ((y) ^ (z))))
#define G(x, y, z) ((y) ^ ((z) & ((x) ^ (y))))
#define H(x, y, z) (((x) ^ (y)) ^ (z))
#define H2(x, y, z) ((x) ^ ((y) ^ (z)))
#define I(x, y, z) ((y) ^ ((x) | ~(z)))

#define STEP(f, a, b, c, d, x, t, s)                          \
  (a) += f((b), (c), (d)) + (x) + (t); \
  (a) = (((a) << (s)) | (((a)&0xffffffff) >> (32 - (s))));    \
  (a) += (b);

static const void *body(Ctx *ctx, const void *data, unsigned long size)
{
  const unsigned char *ptr = (const unsigned char *)data;
  u32 a = ctx->a, b = ctx->b, c = ctx->c, d = ctx->d, saved_a, saved_b, saved_c, saved_d;
#define SET(n)                                                                 \
  (ctx->block[(n)] = (u32)ptr[(n)*4] | ((u32)ptr[(n)*4 + 1] << 8) |          \
                     ((u32)ptr[(n)*4 + 2] << 16) | ((u32)ptr[(n)*4 + 3] << 24))
#define GET(n) (ctx->block[(n)])
  do
  {
    saved_a = a;
    saved_b = b;
    saved_c = c;
    saved_d = d;
    STEP(F, a, b, c, d, SET(0), 0xd76aa478, 7)
    STEP(F, d, a, b, c, SET(1), 0xe8c7b756, 12)
    STEP(F, c, d, a, b, SET(2), 0x242070db, 17)
    STEP(F, b, c, d, a, SET(3), 0xc1bdceee, 22)
    STEP(F, a, b, c, d, SET(4), 0xf57c0faf, 7)
    STEP(F, d, a, b, c, SET(5), 0x4787c62a, 12)
    STEP(F, c, d, a, b, SET(6), 0xa8304613, 17)
    STEP(F, b, c, d, a, SET(7), 0xfd469501, 22)
    STEP(F, a, b, c, d, SET(8), 0x698098d8, 7)
    STEP(F, d, a, b, c, SET(9), 0x8b44f7af, 12)
    STEP(F, c, d, a, b, SET(10), 0xffff5bb1, 17)
    STEP(F, b, c, d, a, SET(11), 0x895cd7be, 22)
    STEP(F, a, b, c, d, SET(12), 0x6b901122, 7)
    STEP(F, d, a, b, c, SET(13), 0xfd987193, 12)
    STEP(F, c, d, a, b, SET(14), 0xa679438e, 17)
    STEP(F, b, c, d, a, SET(15), 0x49b40821, 22)

    STEP(G, a, b, c, d, GET(1), 0xf61e2562, 5)
    STEP(G, d, a, b, c, GET(6), 0xc040b340, 9)
    STEP(G, c, d, a, b, GET(11), 0x265e5a51, 14)
    STEP(G, b, c, d, a, GET(0), 0xe9b6c7aa, 20)
    STEP(G, a, b, c, d, GET(5), 0xd62f105d, 5)
    STEP(G, d, a, b, c, GET(10), 0x02441453, 9)
    STEP(G, c, d, a, b, GET(15), 0xd8a1e681, 14)
    STEP(G, b, c, d, a, GET(4), 0xe7d3fbc8, 20)
    STEP(G, a, b, c, d, GET(9), 0x21e1cde6, 5)
    STEP(G, d, a, b, c, GET(14), 0xc33707d6, 9)
    STEP(G, c, d, a, b, GET(3), 0xf4d50d87, 14)
    STEP(G, b, c, d, a, GET(8), 0x455a14ed, 20)
    STEP(G, a, b, c, d, GET(13), 0xa9e3e905, 5)
    STEP(G, d, a, b, c, GET(2), 0xfcefa3f8, 9)
    STEP(G, c, d, a, b, GET(7), 0x676f02d9, 14)
    STEP(G, b, c, d, a, GET(12), 0x8d2a4c8a, 20)

    STEP(H, a, b, c, d, GET(5), 0xfffa3942, 4)
    STEP(H2, d, a, b, c, GET(8), 0x8771f681, 11)
    STEP(H, c, d, a, b, GET(11), 0x6d9d6122, 16)
    STEP(H2, b, c, d, a, GET(14), 0xfde5380c, 23)
    STEP(H, a, b, c, d, GET(1), 0xa4beea44, 4)
    STEP(H2, d, a, b, c, GET(4), 0x4bdecfa9, 11)
    STEP(H, c, d, a, b, GET(7), 0xf6bb4b60, 16)
    STEP(H2, b, c, d, a, GET(10), 0xbebfbc70, 23)
    STEP(H, a, b, c, d, GET(13), 0x289b7ec6, 4)
    STEP(H2, d, a, b, c, GET(0), 0xeaa127fa, 11)
    STEP(H, c, d, a, b, GET(3), 0xd4ef3085, 16)
    STEP(H2, b, c, d, a, GET(6), 0x04881d05, 23)
    STEP(H, a, b, c, d, GET(9), 0xd9d4d039, 4)
    STEP(H2, d, a, b, c, GET(12), 0xe6db99e5, 11)
    STEP(H, c, d, a, b, GET(15), 0x1fa27cf8, 16)
    STEP(H2, b, c, d, a, GET(2), 0xc4ac5665, 23)

    STEP(I, a, b, c, d, GET(0), 0xf4292244, 6)
    STEP(I, d, a, b, c, GET(7), 0x432aff97, 10)
    STEP(I, c, d, a, b, GET(14), 0xab9423a7, 15)
    STEP(I, b, c, d, a, GET(5), 0xfc93a039, 21)
    STEP(I, a, b, c, d, GET(12), 0x655b59c3, 6)
    STEP(I, d, a, b, c, GET(3), 0x8f0ccc92, 10)
    STEP(I, c, d, a, b, GET(10), 0xffeff47d, 15)
    STEP(I, b, c, d, a, GET(1), 0x85845dd1, 21)
    STEP(I, a, b, c, d, GET(8), 0x6fa87e4f, 6)
    STEP(I, d, a, b, c, GET(15), 0xfe2ce6e0, 10)
    STEP(I, c, d, a, b, GET(6), 0xa3014314, 15)
    STEP(I, b, c, d, a, GET(13), 0x4e0811a1, 21)
    STEP(I, a, b, c, d, GET(4), 0xf7537e82, 6)
    STEP(I, d, a, b, c, GET(11), 0xbd3af235, 10)
    STEP(I, c, d, a, b, GET(2), 0x2ad7d2bb, 15)
    STEP(I, b, c, d, a, GET(9), 0xeb86d391, 21)

    a += saved_a;
    b += saved_b;
    c += saved_c;
    d += saved_d;
    ptr += 64;
  } while(size -= 64);

  ctx->a = a;
  ctx->b = b;
  ctx->c = c;
  ctx->d = d;
  return ptr;
}

static void Init(Ctx *ctx)
{
  ctx->a = 0x67452301;
  ctx->b = 0xefcdab89;
  ctx->c = 0x98badcfe;
  ctx->d = 0x10325476;
  ctx->lo = 0;
  ctx->hi = 0;
}

static void Update(Ctx *ctx, const void *data, unsigned long size)
{
  u32 saved_lo;
  unsigned long used, available;
  saved_lo = ctx->lo;
  if((ctx->lo = (saved_lo + size) & 0x1fffffff) < saved_lo)
    ctx->hi++;
  ctx->hi += (u32)(size >> 29);
  used = saved_lo & 0x3f;
  if(used)
  {
    available = 64 - used;
    if(size < available)
    {
      memcpy(&ctx->buffer[used], data, size);
      return;
    }
    memcpy(&ctx->buffer[used], data, available);
    data = (const unsigned char *)data + available;
    size -= available;
    body(ctx, ctx->buffer, 64);
  }
  if(size >= 64)
  {
    data = body(ctx, data, size & ~(unsigned long)0x3f);
    size &= 0x3f;
  }
  memcpy(ctx->buffer, data, size);
}

static void Final(unsigned char *result, Ctx *ctx)
{
  unsigned long used, available;
  used = ctx->lo & 0x3f;
  ctx->buffer[used++] = 0x80;
  available = 64 - used;
  if(available < 8)
  {
    memset(&ctx->buffer[used], 0, available);
    body(ctx, ctx->buffer, 64);
    used = 0;
    available = 64;
  }
  memset(&ctx->buffer[used], 0, available - 8);
  ctx->lo <<= 3;
  ctx->buffer[56] = (unsigned char)(ctx->lo);
  ctx->buffer[57] = (unsigned char)(ctx->lo >> 8);
  ctx->buffer[58] = (unsigned char)(ctx->lo >> 16);
  ctx->buffer[59] = (unsigned char)(ctx->lo >> 24);
  ctx->buffer[60] = (unsigned char)(ctx->hi);
  ctx->buffer[61] = (unsigned char)(ctx->hi >> 8);
  ctx->buffer[62] = (unsigned char)(ctx->hi >> 16);
  ctx->buffer[63] = (unsigned char)(ctx->hi >> 24);
  body(ctx, ctx->buffer, 64);
  result[0] = (unsigned char)ctx->a;
  result[1] = (unsigned char)(ctx->a >> 8);
  result[2] = (unsigned char)(ctx->a >> 16);
  result[3] = (unsigned char)(ctx->a >> 24);
  result[4] = (unsigned char)ctx->b;
  result[5] = (unsigned char)(ctx->b >> 8);
  result[6] = (unsigned char)(ctx->b >> 16);
  result[7] = (unsigned char)(ctx->b >> 24);
  result[8] = (unsigned char)ctx->c;
  result[9] = (unsigned char)(ctx->c >> 8);
  result[10] = (unsigned char)(ctx->c >> 16);
  result[11] = (unsigned char)(ctx->c >> 24);
  result[12] = (unsigned char)ctx->d;
  result[13] = (unsigned char)(ctx->d >> 8);
  result[14] = (unsigned char)(ctx->d >> 16);
  result[15] = (unsigned char)(ctx->d >> 24);
  memset(ctx, 0, sizeof(*ctx));
}
#undef F
#undef G
#undef H
#undef H2
#undef I
#undef SET
#undef GET
#undef STEP

static std::string Hex(const void *data, unsigned long size)
{
  Ctx c;
  unsigned char digest[16];
  Init(&c);
  Update(&c, data, size);
  Final(digest, &c);
  static const char *hex = "0123456789abcdef";
  std::string out(32, ' ');
  for(int i = 0; i < 16; i++)
  {
    out[i * 2] = hex[digest[i] >> 4];
    out[i * 2 + 1] = hex[digest[i] & 0xf];
  }
  return out;
}
}    // namespace automd5

// ---------------------------------------------------------------------------
// Small helpers shared by both subcommands
// ---------------------------------------------------------------------------

static std::string ResourceIdStr(ResourceId id)
{
  if(id == ResourceId())
    return std::string();
  // DoStringise<ResourceId> lives in renderdoc.dll's core.cpp but isn't
  // exported, so we format manually. ResourceId is bit-identical to uint64_t.
  uint64_t v = 0;
  memcpy(&v, &id, sizeof(v));
  char buf[40];
  snprintf(buf, sizeof(buf), "ResourceId::%llu", (unsigned long long)v);
  return std::string(buf);
}

static const char *DescriptorTypeName(DescriptorType t)
{
  switch(t)
  {
    case DescriptorType::Unknown: return "Unknown";
    case DescriptorType::Sampler: return "Sampler";
    case DescriptorType::ConstantBuffer: return "ConstantBuffer";
    case DescriptorType::ImageSampler: return "ImageSampler";
    case DescriptorType::Image: return "Image";
    case DescriptorType::TypedBuffer: return "TypedBuffer";
    case DescriptorType::Buffer: return "Buffer";
    case DescriptorType::ReadWriteImage: return "ReadWriteImage";
    case DescriptorType::ReadWriteTypedBuffer: return "ReadWriteTypedBuffer";
    case DescriptorType::ReadWriteBuffer: return "ReadWriteBuffer";
    case DescriptorType::AccelerationStructure: return "AccelerationStructure";
    default: return "Other";
  }
}

static const char *DescriptorCategoryName(DescriptorCategory c)
{
  switch(c)
  {
    case DescriptorCategory::Unknown: return "Unknown";
    case DescriptorCategory::Sampler: return "Sampler";
    case DescriptorCategory::ConstantBlock: return "ConstantBlock";
    case DescriptorCategory::ReadOnlyResource: return "ReadOnlyResource";
    case DescriptorCategory::ReadWriteResource: return "ReadWriteResource";
    default: return "Other";
  }
}

static const char *ShaderStageName(ShaderStage s)
{
  switch(s)
  {
    case ShaderStage::Vertex: return "Vertex";
    case ShaderStage::Hull: return "Hull";
    case ShaderStage::Domain: return "Domain";
    case ShaderStage::Geometry: return "Geometry";
    case ShaderStage::Pixel: return "Pixel";
    case ShaderStage::Compute: return "Compute";
    case ShaderStage::Amplification: return "Amplification";
    case ShaderStage::Mesh: return "Mesh";
    default: return "Unknown";
  }
}

static void MkdirP(const std::string &p)
{
  if(p.empty())
    return;
  // create parents first
  std::string cur;
  for(size_t i = 0; i <= p.size(); i++)
  {
    char c = (i < p.size()) ? p[i] : '/';
    if(c == '/' || c == '\\')
    {
      if(!cur.empty() && cur != "." && cur != "..")
        MKDIR(cur.c_str());
    }
    if(i < p.size())
      cur.push_back(c);
  }
  MKDIR(p.c_str());
}

// Collect each shader stage's reflection from the unified PipeState
struct PerStageRefs
{
  const ShaderReflection *refl[(int)ShaderStage::Count] = {NULL};
};

static PerStageRefs CollectShaderRefs(const PipeState &pipe)
{
  PerStageRefs out;
  for(int i = 0; i < (int)ShaderStage::Count; i++)
  {
    out.refl[i] = pipe.GetShaderReflection((ShaderStage)i);
  }
  return out;
}

static void EmitDescriptor(autoj::Writer &w, const Descriptor &d)
{
  w.Key("resource");
  std::string r = ResourceIdStr(d.resource);
  if(r.empty())
    w.Null();
  else
    w.String(r);

  w.Key("view");
  std::string v = ResourceIdStr(d.view);
  if(v.empty())
    w.Null();
  else
    w.String(v);

  w.Key("format");
  w.String(conv(d.format.Name()).c_str());
  w.Key("firstMip");
  w.Int(d.firstMip);
  w.Key("numMips");
  w.Int(d.numMips);
  w.Key("firstSlice");
  w.Int(d.firstSlice);
  w.Key("numSlices");
  w.Int(d.numSlices);
  w.Key("bufferByteOffset");
  w.UInt(d.byteOffset);
  w.Key("bufferByteSize");
  w.UInt(d.byteSize);
}

// Look up the ShaderResource/Sampler/ConstantBlock at `index` in the reflection,
// honoring the descriptor category so we pick the right array.
static const ShaderResource *FindShaderResource(const ShaderReflection *refl,
                                                DescriptorType type, uint16_t index)
{
  if(refl == NULL || index == 0xFFFF)
    return NULL;
  DescriptorCategory cat = CategoryForDescriptorType(type);
  if(cat == DescriptorCategory::ReadOnlyResource && index < refl->readOnlyResources.size())
    return &refl->readOnlyResources[index];
  if(cat == DescriptorCategory::ReadWriteResource && index < refl->readWriteResources.size())
    return &refl->readWriteResources[index];
  return NULL;
}

static const ConstantBlock *FindConstantBlock(const ShaderReflection *refl, uint16_t index)
{
  if(refl == NULL || index == 0xFFFF)
    return NULL;
  if(index < refl->constantBlocks.size())
    return &refl->constantBlocks[index];
  return NULL;
}

static const ShaderSampler *FindShaderSampler(const ShaderReflection *refl, uint16_t index)
{
  if(refl == NULL || index == 0xFFFF)
    return NULL;
  if(index < refl->samplers.size())
    return &refl->samplers[index];
  return NULL;
}

static void EmitBinding(autoj::Writer &w, const UsedDescriptor &used,
                        const PerStageRefs &refs)
{
  const DescriptorAccess &access = used.access;
  const ShaderReflection *refl = NULL;
  if((int)access.stage < (int)ShaderStage::Count)
    refl = refs.refl[(int)access.stage];

  int32_t fixedReg = -1;
  int32_t fixedSpace = -1;
  std::string bindName;
  DescriptorCategory cat = CategoryForDescriptorType(access.type);
  if(refl != NULL && access.index != DescriptorAccess::NoShaderBinding)
  {
    if(cat == DescriptorCategory::ConstantBlock)
    {
      const ConstantBlock *b = FindConstantBlock(refl, access.index);
      if(b != NULL)
      {
        fixedReg = (int32_t)b->fixedBindNumber;
        fixedSpace = (int32_t)b->fixedBindSetOrSpace;
        bindName = conv(b->name);
      }
    }
    else if(cat == DescriptorCategory::Sampler)
    {
      const ShaderSampler *s = FindShaderSampler(refl, access.index);
      if(s != NULL)
      {
        fixedReg = (int32_t)s->fixedBindNumber;
        fixedSpace = (int32_t)s->fixedBindSetOrSpace;
        bindName = conv(s->name);
      }
    }
    else
    {
      const ShaderResource *r = FindShaderResource(refl, access.type, access.index);
      if(r != NULL)
      {
        fixedReg = (int32_t)r->fixedBindNumber;
        fixedSpace = (int32_t)r->fixedBindSetOrSpace;
        bindName = conv(r->name);
      }
    }
  }

  w.BeginObject();
  w.Key("stage");
  w.String(ShaderStageName(access.stage));
  w.Key("type");
  w.String(DescriptorTypeName(access.type));
  w.Key("register");
  if(fixedReg < 0)
    w.Null();
  else
    w.Int(fixedReg);
  w.Key("space");
  if(fixedSpace < 0)
    w.Null();
  else
    w.Int(fixedSpace);
  w.Key("name");
  if(bindName.empty())
    w.Null();
  else
    w.String(bindName);
  w.Key("arrayElement");
  w.UInt(access.arrayElement);
  w.Key("staticallyUnused");
  w.Bool(access.staticallyUnused);
  w.Key("heap");
  std::string heap = ResourceIdStr(access.descriptorStore);
  if(heap.empty())
    w.Null();
  else
    w.String(heap);
  w.Key("heapByteOffset");
  w.UInt(access.byteOffset);
  w.Key("byteSize");
  w.UInt(access.byteSize);
  EmitDescriptor(w, used.descriptor);
  w.EndObject();
}

static void EmitD3D12RootSig(autoj::Writer &w, const D3D12Pipe::State *d3d12)
{
  w.Key("rootSignature");
  w.BeginObject();
  w.Key("id");
  std::string rsid = ResourceIdStr(d3d12->rootSignature.resourceId);
  if(rsid.empty())
    w.Null();
  else
    w.String(rsid);
  w.Key("parameters");
  w.BeginArray();
  for(size_t i = 0; i < d3d12->rootSignature.parameters.size(); i++)
  {
    const D3D12Pipe::RootParam &p = d3d12->rootSignature.parameters[i];
    w.BeginObject();
    w.Key("index");
    w.UInt(i);
    w.Key("space");
    w.UInt(p.space);
    w.Key("reg");
    w.UInt(p.reg);
    if(!p.constants.empty())
    {
      w.Key("kind");
      w.String("RootConstants");
      w.Key("byteSize");
      w.UInt(p.constants.size());
    }
    else if(!p.tableRanges.empty())
    {
      w.Key("kind");
      w.String("RootTable");
      w.Key("heap");
      std::string heap = ResourceIdStr(p.heap);
      if(heap.empty())
        w.Null();
      else
        w.String(heap);
      w.Key("heapByteOffset");
      w.UInt(p.heapByteOffset);
      w.Key("ranges");
      w.BeginArray();
      for(const D3D12Pipe::RootTableRange &r : p.tableRanges)
      {
        w.BeginObject();
        w.Key("category");
        w.String(DescriptorCategoryName(r.category));
        w.Key("space");
        w.UInt(r.space);
        w.Key("baseRegister");
        w.UInt(r.baseRegister);
        w.Key("count");
        w.UInt(r.count);
        w.Key("tableByteOffset");
        w.UInt(r.tableByteOffset);
        w.Key("appended");
        w.Bool(r.appended);
        w.EndObject();
      }
      w.EndArray();
    }
    else
    {
      w.Key("kind");
      w.String("RootDescriptor");
      EmitDescriptor(w, p.descriptor);
    }
    w.EndObject();
  }
  w.EndArray();
  w.EndObject();
}

static void EmitStateAtEvent(autoj::Writer &w, IReplayController *renderer, uint32_t eid)
{
  renderer->SetFrameEvent(eid, true);
  const PipeState &pipe = renderer->GetPipelineState();
  PerStageRefs refs = CollectShaderRefs(pipe);
  const D3D12Pipe::State *d3d12 = renderer->GetD3D12PipelineState();

  w.BeginObject();
  w.Key("eventId");
  w.UInt(eid);

  // Shaders
  w.Key("shaders");
  w.BeginArray();
  for(int i = 0; i < (int)ShaderStage::Count; i++)
  {
    const ShaderReflection *refl = refs.refl[i];
    if(refl == NULL)
      continue;
    w.BeginObject();
    w.Key("stage");
    w.String(ShaderStageName((ShaderStage)i));
    w.Key("shaderId");
    std::string sid = ResourceIdStr(refl->resourceId);
    if(sid.empty())
      w.Null();
    else
      w.String(sid);
    w.Key("entryPoint");
    w.String(conv(refl->entryPoint));
    w.Key("encoding");
    w.String(conv(ToStr(refl->encoding)).c_str());
    w.Key("bytecodeSize");
    w.UInt(refl->rawBytes.size());
    w.Key("bytecodeHash");
    if(refl->rawBytes.empty())
      w.String("empty");
    else
      w.String(automd5::Hex(refl->rawBytes.data(), (unsigned long)refl->rawBytes.size()));
    w.EndObject();
  }
  w.EndArray();

  // D3D12-specific bits
  if(d3d12 && d3d12->pipelineResourceId != ResourceId())
  {
    w.Key("api");
    w.String("D3D12");
    w.Key("pipelineId");
    w.String(ResourceIdStr(d3d12->pipelineResourceId));
    w.Key("descriptorHeaps");
    w.BeginArray();
    for(ResourceId h : d3d12->descriptorHeaps)
    {
      std::string s = ResourceIdStr(h);
      if(!s.empty())
        w.String(s);
    }
    w.EndArray();
    w.Key("viewports");
    w.BeginArray();
    for(const Viewport &v : d3d12->rasterizer.viewports)
    {
      w.BeginObject();
      w.Key("x");
      w.Double(v.x);
      w.Key("y");
      w.Double(v.y);
      w.Key("width");
      w.Double(v.width);
      w.Key("height");
      w.Double(v.height);
      w.Key("minDepth");
      w.Double(v.minDepth);
      w.Key("maxDepth");
      w.Double(v.maxDepth);
      w.EndObject();
    }
    w.EndArray();
    w.Key("scissors");
    w.BeginArray();
    for(const Scissor &s : d3d12->rasterizer.scissors)
    {
      w.BeginObject();
      w.Key("x");
      w.Int(s.x);
      w.Key("y");
      w.Int(s.y);
      w.Key("width");
      w.Int(s.width);
      w.Key("height");
      w.Int(s.height);
      w.EndObject();
    }
    w.EndArray();
    w.Key("renderTargets");
    w.BeginArray();
    for(const Descriptor &d : d3d12->outputMerger.renderTargets)
    {
      if(ResourceIdStr(d.resource).empty())
        continue;
      w.BeginObject();
      EmitDescriptor(w, d);
      w.EndObject();
    }
    w.EndArray();
    EmitD3D12RootSig(w, d3d12);
  }

  // Bindings — descriptor → resource resolution per stage
  w.Key("bindings");
  w.BeginArray();
  for(int s = 0; s < (int)ShaderStage::Count; s++)
  {
    ShaderStage stg = (ShaderStage)s;
    rdcarray<UsedDescriptor> a = pipe.GetReadOnlyResources(stg, false);
    for(const UsedDescriptor &u : a)
      EmitBinding(w, u, refs);
    a = pipe.GetReadWriteResources(stg, false);
    for(const UsedDescriptor &u : a)
      EmitBinding(w, u, refs);
    a = pipe.GetConstantBlocks(stg, false);
    for(const UsedDescriptor &u : a)
      EmitBinding(w, u, refs);
    a = pipe.GetSamplers(stg, false);
    for(const UsedDescriptor &u : a)
      EmitBinding(w, u, refs);
  }
  w.EndArray();

  w.EndObject();
}

static void GatherActionFlags(autoj::Writer &w, ActionFlags flags)
{
  w.BeginArray();
  struct
  {
    ActionFlags bit;
    const char *name;
  } kFlags[] = {
      {ActionFlags::Clear, "Clear"},
      {ActionFlags::Drawcall, "Drawcall"},
      {ActionFlags::Dispatch, "Dispatch"},
      {ActionFlags::CmdList, "CmdList"},
      {ActionFlags::SetMarker, "SetMarker"},
      {ActionFlags::PushMarker, "PushMarker"},
      {ActionFlags::PopMarker, "PopMarker"},
      {ActionFlags::Present, "Present"},
      {ActionFlags::MultiAction, "MultiAction"},
      {ActionFlags::Copy, "Copy"},
      {ActionFlags::Resolve, "Resolve"},
      {ActionFlags::GenMips, "GenMips"},
      {ActionFlags::PassBoundary, "PassBoundary"},
      {ActionFlags::Indexed, "Indexed"},
      {ActionFlags::Instanced, "Instanced"},
      {ActionFlags::Auto, "Auto"},
      {ActionFlags::Indirect, "Indirect"},
      {ActionFlags::ClearColor, "ClearColor"},
      {ActionFlags::ClearDepthStencil, "ClearDepthStencil"},
      {ActionFlags::BeginPass, "BeginPass"},
      {ActionFlags::EndPass, "EndPass"},
      {ActionFlags::CommandBufferBoundary, "CommandBufferBoundary"},
      {ActionFlags::MeshDispatch, "MeshDispatch"},
  };
  for(auto &f : kFlags)
  {
    if((uint32_t)flags & (uint32_t)f.bit)
      w.String(f.name);
  }
  w.EndArray();
}

static void WalkActions(const ActionDescription &a, const SDFile &sdfile,
                        autoj::Writer &events, autoj::Writer &actions, autoj::Writer &state,
                        IReplayController *renderer, std::set<std::string> *seenShaders,
                        const std::string &shadersDir, uint32_t parentEid,
                        uint32_t *eventCount, uint32_t *actionCount)
{
  uint32_t eid = a.eventId;
  (*eventCount)++;

  events.BeginObject();
  events.Key("eventId");
  events.UInt(eid);
  events.Key("actionId");
  events.UInt(a.actionId);
  events.Key("name");
  events.String(conv(a.GetName(sdfile)));
  events.Key("flags");
  GatherActionFlags(events, a.flags);
  events.Key("parentEventId");
  if(parentEid == 0)
    events.Null();
  else
    events.UInt(parentEid);
  events.Key("numChildren");
  events.UInt(a.children.size());
  events.EndObject();
  events.NewlineRecord();

  bool significant = (((uint32_t)a.flags &
                       ((uint32_t)ActionFlags::Drawcall | (uint32_t)ActionFlags::Dispatch |
                        (uint32_t)ActionFlags::Copy | (uint32_t)ActionFlags::Resolve |
                        (uint32_t)ActionFlags::Clear | (uint32_t)ActionFlags::GenMips |
                        (uint32_t)ActionFlags::MeshDispatch | (uint32_t)ActionFlags::Indirect)) != 0);

  if(significant)
  {
    (*actionCount)++;
    actions.BeginObject();
    actions.Key("eventId");
    actions.UInt(eid);
    actions.Key("name");
    actions.String(conv(a.GetName(sdfile)));
    actions.Key("flags");
    GatherActionFlags(actions, a.flags);
    actions.Key("numIndices");
    actions.UInt(a.numIndices);
    actions.Key("numInstances");
    actions.UInt(a.numInstances);
    actions.Key("indexOffset");
    actions.UInt(a.indexOffset);
    actions.Key("vertexOffset");
    actions.UInt(a.vertexOffset);
    actions.Key("instanceOffset");
    actions.UInt(a.instanceOffset);
    actions.Key("baseVertex");
    actions.Int(a.baseVertex);
    actions.Key("dispatchDim");
    actions.BeginArray();
    actions.UInt(a.dispatchDimension[0]);
    actions.UInt(a.dispatchDimension[1]);
    actions.UInt(a.dispatchDimension[2]);
    actions.EndArray();
    actions.Key("copySource");
    if(ResourceIdStr(a.copySource).empty())
      actions.Null();
    else
      actions.String(ResourceIdStr(a.copySource));
    actions.Key("copyDestination");
    if(ResourceIdStr(a.copyDestination).empty())
      actions.Null();
    else
      actions.String(ResourceIdStr(a.copyDestination));
    actions.Key("outputs");
    actions.BeginArray();
    for(ResourceId o : a.outputs)
    {
      std::string s = ResourceIdStr(o);
      if(!s.empty())
        actions.String(s);
    }
    actions.EndArray();
    actions.Key("depthOut");
    if(ResourceIdStr(a.depthOut).empty())
      actions.Null();
    else
      actions.String(ResourceIdStr(a.depthOut));
    actions.EndObject();
    actions.NewlineRecord();

    // Snapshot per-event state
    EmitStateAtEvent(state, renderer, eid);
    state.NewlineRecord();

    // Save shader bytecode for unique hashes
    const PipeState &pipe = renderer->GetPipelineState();
    for(int i = 0; i < (int)ShaderStage::Count; i++)
    {
      const ShaderReflection *refl = pipe.GetShaderReflection((ShaderStage)i);
      if(refl == NULL || refl->rawBytes.empty())
        continue;
      std::string h = automd5::Hex(refl->rawBytes.data(), (unsigned long)refl->rawBytes.size());
      if(seenShaders->count(h) > 0)
        continue;
      seenShaders->insert(h);
      std::string sub = shadersDir + "/" + h.substr(0, 2);
      MkdirP(sub);
      std::string binPath = sub + "/" + h + ".bin";
      FILE *f = fopen(binPath.c_str(), "wb");
      if(f)
      {
        fwrite(refl->rawBytes.data(), 1, refl->rawBytes.size(), f);
        fclose(f);
      }
    }
  }

  for(const ActionDescription &child : a.children)
    WalkActions(child, sdfile, events, actions, state, renderer, seenShaders, shadersDir, eid,
                eventCount, actionCount);
}

// ---------------------------------------------------------------------------
// IndexCaptureCommand
// ---------------------------------------------------------------------------

struct IndexCaptureCommand : public Command
{
private:
  std::string filename;
  std::string outdir;

public:
  IndexCaptureCommand() : Command() {}
  virtual void AddOptions(cmdline::parser &parser)
  {
    parser.set_footer("<capture.rdc>");
    parser.add<std::string>("out", 'o', "Output directory for the index.", true, "");
  }
  virtual const char *Description()
  {
    return "Walk a capture and emit JSONL index files (events, actions, state, bindings).";
  }
  virtual bool IsInternalOnly() { return false; }
  virtual bool IsCaptureCommand() { return false; }
  virtual bool Parse(cmdline::parser &parser, GlobalEnvironment &)
  {
    std::vector<std::string> rest = parser.rest();
    if(rest.empty())
    {
      std::cerr << "Error: index-capture needs a capture filename." << std::endl
                << std::endl
                << parser.usage();
      return false;
    }
    filename = rest[0];
    rest.erase(rest.begin());
    parser.set_rest(rest);
    outdir = parser.get<std::string>("out");
    if(outdir.empty())
    {
      std::cerr << "Error: --out is required." << std::endl;
      return false;
    }
    return true;
  }
  virtual int Execute(const CaptureOptions &)
  {
    ICaptureFile *file = RENDERDOC_OpenCaptureFile();
    ResultDetails res = file->OpenFile(conv(filename), "rdc", NULL);
    if(res.code != ResultCode::Succeeded)
    {
      std::cerr << "Couldn't open '" << filename << "': " << res.Message() << std::endl;
      return 1;
    }

    IReplayController *renderer = NULL;
    ResultDetails result = {};
    rdctie(result, renderer) = file->OpenCapture(ReplayOptions(), NULL);
    file->Shutdown();
    if(!result.OK())
    {
      std::cerr << "Couldn't replay '" << filename << "': " << result.Message() << std::endl;
      return 1;
    }

    MkdirP(outdir);
    std::string shadersDir = outdir + "/shaders";
    MkdirP(shadersDir);

    // meta.json
    {
      std::ofstream meta((outdir + "/meta.json").c_str(), std::ios::out | std::ios::trunc);
      APIProperties props = renderer->GetAPIProperties();
      meta << "{\n  \"indexer_version\": \"0.1.0\",\n";
      meta << "  \"capture_path\": \"" << filename << "\",\n";
      meta << "  \"api\": \"" << conv(ToStr(props.pipelineType)) << "\",\n";
      meta << "  \"vendor\": \"" << conv(ToStr(props.vendor)) << "\"\n}\n";
    }

    // resources.json
    {
      std::ofstream rf((outdir + "/resources.json").c_str(), std::ios::out | std::ios::trunc);
      autoj::Writer w(&rf, false);
      w.BeginObject();
      w.Key("resources");
      w.BeginArray();
      for(const ResourceDescription &r : renderer->GetResources())
      {
        w.BeginObject();
        w.Key("id");
        w.String(ResourceIdStr(r.resourceId));
        w.Key("name");
        w.String(conv(r.name));
        w.Key("type");
        w.String(conv(ToStr(r.type)).c_str());
        w.EndObject();
      }
      w.EndArray();
      w.Key("textures");
      w.BeginArray();
      for(const TextureDescription &t : renderer->GetTextures())
      {
        w.BeginObject();
        w.Key("id");
        w.String(ResourceIdStr(t.resourceId));
        w.Key("type");
        w.String(conv(ToStr(t.type)).c_str());
        w.Key("format");
        w.String(conv(t.format.Name()).c_str());
        w.Key("width");
        w.UInt(t.width);
        w.Key("height");
        w.UInt(t.height);
        w.Key("depth");
        w.UInt(t.depth);
        w.Key("mips");
        w.UInt(t.mips);
        w.Key("arraysize");
        w.UInt(t.arraysize);
        w.Key("samples");
        w.UInt(t.msSamp);
        w.Key("byteSize");
        w.UInt(t.byteSize);
        w.EndObject();
      }
      w.EndArray();
      w.Key("buffers");
      w.BeginArray();
      for(const BufferDescription &b : renderer->GetBuffers())
      {
        w.BeginObject();
        w.Key("id");
        w.String(ResourceIdStr(b.resourceId));
        w.Key("length");
        w.UInt(b.length);
        w.EndObject();
      }
      w.EndArray();
      w.EndObject();
      w.Flush();
    }

    // events / actions / state
    std::ofstream ev((outdir + "/events.jsonl").c_str(), std::ios::out | std::ios::trunc);
    std::ofstream ac((outdir + "/actions.jsonl").c_str(), std::ios::out | std::ios::trunc);
    std::ofstream st((outdir + "/state.jsonl").c_str(), std::ios::out | std::ios::trunc);
    autoj::Writer evW(&ev, false), acW(&ac, false), stW(&st, false);

    std::set<std::string> seenShaders;
    uint32_t eventCount = 0, actionCount = 0;
    const SDFile &sdfile = renderer->GetStructuredFile();
    for(const ActionDescription &root : renderer->GetRootActions())
    {
      WalkActions(root, sdfile, evW, acW, stW, renderer, &seenShaders, shadersDir, 0,
                  &eventCount, &actionCount);
    }

    evW.Flush();
    acW.Flush();
    stW.Flush();

    std::cout << "Indexed " << eventCount << " events, " << actionCount
              << " actions, " << seenShaders.size() << " unique shaders -> " << outdir
              << std::endl;

    renderer->Shutdown();
    return 0;
  }
};

// ---------------------------------------------------------------------------
// StateAtEventCommand
// ---------------------------------------------------------------------------

struct StateAtEventCommand : public Command
{
private:
  std::string filename;
  uint32_t eventId = 0;
  std::string outfile;

public:
  StateAtEventCommand() : Command() {}
  virtual void AddOptions(cmdline::parser &parser)
  {
    parser.set_footer("<capture.rdc>");
    parser.add<uint32_t>("event", 'e', "Event ID to inspect.", true, 0);
    parser.add<std::string>("out", 'o', "Optional output file (default stdout).", false, "");
  }
  virtual const char *Description()
  {
    return "Dump pipeline state at a specific event as JSON.";
  }
  virtual bool IsInternalOnly() { return false; }
  virtual bool IsCaptureCommand() { return false; }
  virtual bool Parse(cmdline::parser &parser, GlobalEnvironment &)
  {
    std::vector<std::string> rest = parser.rest();
    if(rest.empty())
    {
      std::cerr << "Error: state-at-event needs a capture filename." << std::endl
                << std::endl
                << parser.usage();
      return false;
    }
    filename = rest[0];
    rest.erase(rest.begin());
    parser.set_rest(rest);
    eventId = parser.get<uint32_t>("event");
    outfile = parser.get<std::string>("out");
    return true;
  }
  virtual int Execute(const CaptureOptions &)
  {
    ICaptureFile *file = RENDERDOC_OpenCaptureFile();
    ResultDetails res = file->OpenFile(conv(filename), "rdc", NULL);
    if(res.code != ResultCode::Succeeded)
    {
      std::cerr << "Couldn't open '" << filename << "': " << res.Message() << std::endl;
      return 1;
    }
    IReplayController *renderer = NULL;
    ResultDetails result = {};
    rdctie(result, renderer) = file->OpenCapture(ReplayOptions(), NULL);
    file->Shutdown();
    if(!result.OK())
    {
      std::cerr << "Couldn't replay '" << filename << "': " << result.Message() << std::endl;
      return 1;
    }

    std::string body;
    {
      autoj::Writer w(&body);
      EmitStateAtEvent(w, renderer, eventId);
    }

    if(outfile.empty())
    {
      std::cout << body << std::endl;
    }
    else
    {
      std::ofstream o(outfile.c_str(), std::ios::out | std::ios::trunc);
      o << body;
    }
    renderer->Shutdown();
    return 0;
  }
};

// ---------------------------------------------------------------------------
// Registration hook called from renderdoccmd.cpp
// ---------------------------------------------------------------------------

void register_automation_commands()
{
  add_command("index-capture", new IndexCaptureCommand());
  add_command("state-at-event", new StateAtEventCommand());
}
